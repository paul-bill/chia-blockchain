from __future__ import annotations

import dataclasses
import logging
import time
import traceback
from secrets import token_bytes
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Iterator, List, Optional, Set, Tuple, Type, cast

from blspy import AugSchemeMPL, G1Element, G2Element
from clvm_tools.binutils import disassemble

from chia.consensus.cost_calculator import NPCResult
from chia.full_node.bundle_tools import simple_solution_generator
from chia.full_node.mempool_check_conditions import get_name_puzzle_conditions
from chia.server.ws_connection import WSChiaConnection
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.generator_types import BlockGenerator
from chia.types.spend_bundle import SpendBundle
from chia.util.byte_types import hexstr_to_bytes
from chia.util.condition_tools import conditions_dict_for_solution, pkm_pairs_for_conditions_dict
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64, uint128
from chia.wallet.action_manager.action_aliases import DirectPayment, OfferedAmount
from chia.wallet.action_manager.protocols import (
    ActionAlias,
    PuzzleDescription,
    SolutionDescription,
    SpendDescription,
    WalletAction,
)
from chia.wallet.action_manager.wallet_actions import Condition
from chia.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from chia.wallet.cat_wallet.cat_info import CATInfo, LegacyCATInfo
from chia.wallet.cat_wallet.cat_utils import (
    SpendableCAT,
    construct_cat_puzzle,
    match_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.cat_wallet.lineage_store import CATLineageStore
from chia.wallet.coin_selection import select_coins
from chia.wallet.derivation_record import DerivationRecord
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.payment import Payment
from chia.wallet.puzzle_drivers import PuzzleInfo, Solver, cast_to_int
from chia.wallet.puzzles.cat_loader import CAT_MOD
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
)
from chia.wallet.puzzles.tails import ALL_LIMITATIONS_PROGRAMS
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.uncurried_puzzle import UncurriedPuzzle, uncurry_puzzle
from chia.wallet.util.compute_memos import compute_memos
from chia.wallet.util.curry_and_treehash import calculate_hash_of_quoted_mod_hash, curry_and_treehash
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.wallet_sync_utils import fetch_coin_spend_for_coin_state
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_coin_record import WalletCoinRecord
from chia.wallet.wallet_info import WalletInfo

if TYPE_CHECKING:
    from chia.wallet.wallet_state_manager import WalletStateManager

# This should probably not live in this file but it's for experimental right now

CAT_MOD_HASH = CAT_MOD.get_tree_hash()
CAT_MOD_HASH_HASH = Program.to(CAT_MOD_HASH).get_tree_hash()
QUOTED_MOD_HASH = calculate_hash_of_quoted_mod_hash(CAT_MOD_HASH)


class CATWallet:
    if TYPE_CHECKING:
        from chia.wallet.wallet_protocol import WalletProtocol

        _protocol_check: ClassVar[WalletProtocol] = cast("CATWallet", None)

    wallet_state_manager: WalletStateManager
    log: logging.Logger
    wallet_info: WalletInfo
    cat_info: CATInfo
    standard_wallet: Wallet
    cost_of_single_tx: Optional[int]
    lineage_store: CATLineageStore

    @staticmethod
    def default_wallet_name_for_unknown_cat(limitations_program_hash_hex: str) -> str:
        return f"CAT {limitations_program_hash_hex[:16]}..."

    @staticmethod
    async def create_new_cat_wallet(
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        cat_tail_info: Dict[str, Any],
        amount: uint64,
        name: Optional[str] = None,
    ) -> "CATWallet":
        self = CATWallet()
        self.cost_of_single_tx = None
        self.standard_wallet = wallet
        self.log = logging.getLogger(__name__)
        std_wallet_id = self.standard_wallet.wallet_id
        bal = await wallet_state_manager.get_confirmed_balance_for_wallet(std_wallet_id)
        if amount > bal:
            raise ValueError("Not enough balance")
        self.wallet_state_manager = wallet_state_manager

        # We use 00 bytes because it's not optional. We must check this is overridden during issuance.
        empty_bytes = bytes32(32 * b"\0")
        self.cat_info = CATInfo(empty_bytes, None)
        info_as_string = bytes(self.cat_info).hex()
        # If the name is not provided, it will be autogenerated based on the resulting tail hash.
        # For now, give the wallet a temporary name "CAT WALLET" until we get the tail hash
        original_name = name
        if name is None:
            name = "CAT WALLET"

        self.wallet_info = await wallet_state_manager.user_store.create_wallet(name, WalletType.CAT, info_as_string)

        try:
            chia_tx, spend_bundle = await ALL_LIMITATIONS_PROGRAMS[
                cat_tail_info["identifier"]
            ].generate_issuance_bundle(
                self,
                cat_tail_info,
                amount,
            )
            assert self.cat_info.limitations_program_hash != empty_bytes
        except Exception:
            await wallet_state_manager.user_store.delete_wallet(self.id())
            raise
        if spend_bundle is None:
            await wallet_state_manager.user_store.delete_wallet(self.id())
            raise ValueError("Failed to create spend.")

        await self.wallet_state_manager.add_new_wallet(self)

        # If the new CAT name wasn't originally provided, we used a temporary name before issuance
        # since we didn't yet know the TAIL. Now we know the TAIL, we can update the name
        # according to the template name for unknown/new CATs.
        if original_name is None:
            name = self.default_wallet_name_for_unknown_cat(self.cat_info.limitations_program_hash.hex())
            await self.set_name(name)

        # Change and actual CAT coin
        non_ephemeral_coins: List[Coin] = spend_bundle.not_ephemeral_additions()
        cat_coin = None
        puzzle_store = self.wallet_state_manager.puzzle_store
        for c in non_ephemeral_coins:
            wallet_identifier = await puzzle_store.get_wallet_identifier_for_puzzle_hash(c.puzzle_hash)
            if wallet_identifier is None:
                raise ValueError("Internal Error")
            if wallet_identifier.id == self.id():
                cat_coin = c

        if cat_coin is None:
            raise ValueError("Internal Error, unable to generate new CAT coin")
        cat_pid: bytes32 = cat_coin.parent_coin_info

        cat_record = TransactionRecord(
            confirmed_at_height=uint32(0),
            created_at_time=uint64(int(time.time())),
            to_puzzle_hash=(await self.convert_puzzle_hash(cat_coin.puzzle_hash)),
            amount=uint64(cat_coin.amount),
            fee_amount=uint64(0),
            confirmed=False,
            sent=uint32(10),
            spend_bundle=None,
            additions=[cat_coin],
            removals=list(filter(lambda rem: rem.name() == cat_pid, spend_bundle.removals())),
            wallet_id=self.id(),
            sent_to=[],
            trade_id=None,
            type=uint32(TransactionType.INCOMING_TX.value),
            name=bytes32(token_bytes()),
            memos=[],
        )
        chia_tx = dataclasses.replace(chia_tx, spend_bundle=spend_bundle)
        await self.standard_wallet.push_transaction(chia_tx)
        await self.standard_wallet.push_transaction(cat_record)
        return self

    @staticmethod
    async def get_or_create_wallet_for_cat(
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        limitations_program_hash_hex: str,
        name: Optional[str] = None,
    ) -> CATWallet:
        self = CATWallet()
        self.cost_of_single_tx = None
        self.standard_wallet = wallet
        self.log = logging.getLogger(__name__)

        limitations_program_hash_hex = bytes32.from_hexstr(limitations_program_hash_hex).hex()  # Normalize the format

        for id, w in wallet_state_manager.wallets.items():
            if w.type() == CATWallet.type():
                assert isinstance(w, CATWallet)
                if w.get_asset_id() == limitations_program_hash_hex:
                    self.log.warning("Not creating wallet for already existing CAT wallet")
                    return w

        self.wallet_state_manager = wallet_state_manager
        if limitations_program_hash_hex in DEFAULT_CATS:
            cat_info = DEFAULT_CATS[limitations_program_hash_hex]
            name = cat_info["name"]
        elif name is None:
            name = self.default_wallet_name_for_unknown_cat(limitations_program_hash_hex)

        limitations_program_hash = bytes32(hexstr_to_bytes(limitations_program_hash_hex))
        self.cat_info = CATInfo(limitations_program_hash, None)
        info_as_string = bytes(self.cat_info).hex()
        self.wallet_info = await wallet_state_manager.user_store.create_wallet(name, WalletType.CAT, info_as_string)

        self.lineage_store = await CATLineageStore.create(self.wallet_state_manager.db_wrapper, self.get_asset_id())
        await self.wallet_state_manager.add_new_wallet(self)
        return self

    @classmethod
    async def create_from_puzzle_info(
        cls,
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        puzzle_driver: PuzzleInfo,
        name: Optional[str] = None,
    ) -> CATWallet:
        return await cls.get_or_create_wallet_for_cat(
            wallet_state_manager,
            wallet,
            puzzle_driver["tail"].hex(),
            name,
        )

    @staticmethod
    async def create(
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        wallet_info: WalletInfo,
    ) -> CATWallet:
        self = CATWallet()

        self.log = logging.getLogger(__name__)

        self.cost_of_single_tx = None
        self.wallet_state_manager = wallet_state_manager
        self.wallet_info = wallet_info
        self.standard_wallet = wallet
        try:
            self.cat_info = CATInfo.from_bytes(hexstr_to_bytes(self.wallet_info.data))
            self.lineage_store = await CATLineageStore.create(self.wallet_state_manager.db_wrapper, self.get_asset_id())
        except AssertionError:
            # Do a migration of the lineage proofs
            cat_info = LegacyCATInfo.from_bytes(hexstr_to_bytes(self.wallet_info.data))
            self.cat_info = CATInfo(cat_info.limitations_program_hash, cat_info.my_tail)
            self.lineage_store = await CATLineageStore.create(self.wallet_state_manager.db_wrapper, self.get_asset_id())
            for coin_id, lineage in cat_info.lineage_proofs:
                await self.add_lineage(coin_id, lineage)
            await self.save_info(self.cat_info)

        return self

    @classmethod
    def type(cls) -> WalletType:
        return WalletType.CAT

    def id(self) -> uint32:
        return self.wallet_info.id

    async def get_confirmed_balance(self, record_list: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        if record_list is None:
            record_list = await self.wallet_state_manager.coin_store.get_unspent_coins_for_wallet(self.id())

        amount: uint128 = uint128(0)
        for record in record_list:
            lineage = await self.get_lineage_proof_for_coin(record.coin)
            if lineage is not None:
                amount = uint128(amount + record.coin.amount)

        self.log.info(f"Confirmed balance for cat wallet {self.id()} is {amount}")
        return uint128(amount)

    async def get_unconfirmed_balance(self, unspent_records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        return await self.wallet_state_manager.get_unconfirmed_balance(self.id(), unspent_records)

    async def get_max_send_amount(self, records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        spendable: List[WalletCoinRecord] = list(await self.get_cat_spendable_coins())
        if len(spendable) == 0:
            return uint128(0)
        spendable.sort(reverse=True, key=lambda record: record.coin.amount)
        if self.cost_of_single_tx is None:
            coin = spendable[0].coin
            txs = await self.generate_signed_transaction(
                [uint64(coin.amount)], [coin.puzzle_hash], coins={coin}, ignore_max_send_amount=True
            )
            assert txs[0].spend_bundle
            program: BlockGenerator = simple_solution_generator(txs[0].spend_bundle)
            # npc contains names of the coins removed, puzzle_hashes and their spend conditions
            # we use height=0 here to not enable any soft-fork semantics. It
            # will only matter once the wallet generates transactions relying on
            # new conditions, and we can change this by then
            result: NPCResult = get_name_puzzle_conditions(
                program, self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM, mempool_mode=True, height=uint32(0)
            )
            self.cost_of_single_tx = result.cost
            self.log.info(f"Cost of a single tx for CAT wallet: {self.cost_of_single_tx}")

        max_cost = self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM / 2  # avoid full block TXs
        current_cost = 0
        total_amount = 0
        total_coin_count = 0

        for record in spendable:
            current_cost += self.cost_of_single_tx
            total_amount += record.coin.amount
            total_coin_count += 1
            if current_cost + self.cost_of_single_tx > max_cost:
                break

        return uint128(total_amount)

    def get_name(self) -> str:
        return self.wallet_info.name

    async def set_name(self, new_name: str) -> None:
        new_info = dataclasses.replace(self.wallet_info, name=new_name)
        self.wallet_info = new_info
        await self.wallet_state_manager.user_store.update_wallet(self.wallet_info)

    def get_asset_id(self) -> str:
        return bytes(self.cat_info.limitations_program_hash).hex()

    async def set_tail_program(self, tail_program: str) -> None:
        assert Program.fromhex(tail_program).get_tree_hash() == self.cat_info.limitations_program_hash
        await self.save_info(
            CATInfo(
                self.cat_info.limitations_program_hash,
                Program.fromhex(tail_program),
            )
        )

    async def coin_added(self, coin: Coin, height: uint32, peer: WSChiaConnection) -> None:
        """Notification from wallet state manager that wallet has been received."""
        self.log.info(f"CAT wallet has been notified that {coin.name().hex()} was added")

        inner_puzzle = await self.inner_puzzle_for_cat_puzhash(coin.puzzle_hash)
        lineage_proof = LineageProof(coin.parent_coin_info, inner_puzzle.get_tree_hash(), uint64(coin.amount))
        await self.add_lineage(coin.name(), lineage_proof)

        lineage = await self.get_lineage_proof_for_coin(coin)

        if lineage is None:
            try:
                coin_state = await self.wallet_state_manager.wallet_node.get_coin_state(
                    [coin.parent_coin_info], peer=peer
                )
                assert coin_state[0].coin.name() == coin.parent_coin_info
                coin_spend = await fetch_coin_spend_for_coin_state(coin_state[0], peer)
                await self.puzzle_solution_received(coin_spend, parent_coin=coin_state[0].coin)
            except Exception as e:
                self.log.debug(f"Exception: {e}, traceback: {traceback.format_exc()}")

    async def puzzle_solution_received(self, coin_spend: CoinSpend, parent_coin: Coin) -> None:
        coin_name = coin_spend.coin.name()
        puzzle: Program = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
        args = match_cat_puzzle(uncurry_puzzle(puzzle))
        if args is not None:
            mod_hash, genesis_coin_checker_hash, inner_puzzle = args
            self.log.info(f"parent: {coin_name.hex()} inner_puzzle for parent is {inner_puzzle}")

            await self.add_lineage(
                coin_name,
                LineageProof(parent_coin.parent_coin_info, inner_puzzle.get_tree_hash(), uint64(parent_coin.amount)),
            )
        else:
            # The parent is not a CAT which means we need to scrub all of its children from our DB
            child_coin_records = await self.wallet_state_manager.coin_store.get_coin_records_by_parent_id(coin_name)
            if len(child_coin_records) > 0:
                for record in child_coin_records:
                    if record.wallet_id == self.id():
                        await self.wallet_state_manager.coin_store.delete_coin_record(record.coin.name())
                        await self.remove_lineage(record.coin.name())
                        # We also need to make sure there's no record of the transaction
                        await self.wallet_state_manager.tx_store.delete_transaction_record(record.coin.name())

    async def get_new_inner_hash(self) -> bytes32:
        puzzle = await self.get_new_inner_puzzle()
        return puzzle.get_tree_hash()

    async def get_new_inner_puzzle(self) -> Program:
        return await self.standard_wallet.get_new_puzzle()

    async def get_new_puzzlehash(self) -> bytes32:
        return await self.standard_wallet.get_new_puzzlehash()

    async def get_puzzle_hash(self, new: bool) -> bytes32:
        if new:
            return await self.get_new_puzzlehash()
        else:
            record: Optional[
                DerivationRecord
            ] = await self.wallet_state_manager.get_current_derivation_record_for_wallet(self.standard_wallet.id())
            if record is None:
                return await self.get_new_puzzlehash()
            return record.puzzle_hash

    def require_derivation_paths(self) -> bool:
        return True

    def puzzle_for_pk(self, pubkey: G1Element) -> Program:
        inner_puzzle = self.standard_wallet.puzzle_for_pk(pubkey)
        cat_puzzle: Program = construct_cat_puzzle(CAT_MOD, self.cat_info.limitations_program_hash, inner_puzzle)
        return cat_puzzle

    def puzzle_hash_for_pk(self, pubkey: G1Element) -> bytes32:
        inner_puzzle_hash = self.standard_wallet.puzzle_hash_for_pk(pubkey)
        limitations_program_hash_hash = Program.to(self.cat_info.limitations_program_hash).get_tree_hash()
        return curry_and_treehash(QUOTED_MOD_HASH, CAT_MOD_HASH_HASH, limitations_program_hash_hash, inner_puzzle_hash)

    async def get_new_cat_puzzle_hash(self) -> bytes32:
        return (await self.wallet_state_manager.get_unused_derivation_record(self.id())).puzzle_hash

    async def get_spendable_balance(self, records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        coins = await self.get_cat_spendable_coins(records)
        amount = 0
        for record in coins:
            amount += record.coin.amount

        return uint128(amount)

    async def get_pending_change_balance(self) -> uint64:
        unconfirmed_tx = await self.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(self.id())
        addition_amount = 0
        for record in unconfirmed_tx:
            if not record.is_in_mempool():
                continue
            our_spend = False
            for coin in record.removals:
                if await self.wallet_state_manager.does_coin_belong_to_wallet(coin, self.id()):
                    our_spend = True
                    break

            if our_spend is not True:
                continue

            for coin in record.additions:
                if await self.wallet_state_manager.does_coin_belong_to_wallet(coin, self.id()):
                    addition_amount += coin.amount

        return uint64(addition_amount)

    async def get_cat_spendable_coins(self, records: Optional[Set[WalletCoinRecord]] = None) -> List[WalletCoinRecord]:
        result: List[WalletCoinRecord] = []

        record_list: Set[WalletCoinRecord] = await self.wallet_state_manager.get_spendable_coins_for_wallet(
            self.id(), records
        )

        for record in record_list:
            lineage = await self.get_lineage_proof_for_coin(record.coin)
            if lineage is not None and not lineage.is_none():
                result.append(record)

        return result

    async def select_coins(
        self,
        amount: uint64,
        exclude: Optional[List[Coin]] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        excluded_coin_amounts: Optional[List[uint64]] = None,
    ) -> Set[Coin]:
        """
        Returns a set of coins that can be used for generating a new transaction.
        Note: Must be called under wallet state manager lock
        """
        spendable_amount: uint128 = await self.get_spendable_balance()
        spendable_coins: List[WalletCoinRecord] = await self.get_cat_spendable_coins()

        # Try to use coins from the store, if there isn't enough of "unused"
        # coins use change coins that are not confirmed yet
        unconfirmed_removals: Dict[bytes32, Coin] = await self.wallet_state_manager.unconfirmed_removals_for_wallet(
            self.id()
        )
        if max_coin_amount is None:
            max_coin_amount = uint64(self.wallet_state_manager.constants.MAX_COIN_AMOUNT)
        coins = await select_coins(
            spendable_amount,
            max_coin_amount,
            spendable_coins,
            unconfirmed_removals,
            self.log,
            uint128(amount),
            exclude,
            min_coin_amount,
            excluded_coin_amounts,
        )
        assert sum(c.amount for c in coins) >= amount
        return coins

    async def sign(self, spend_bundle: SpendBundle) -> SpendBundle:
        sigs: List[G2Element] = []
        for spend in spend_bundle.coin_spends:
            args = match_cat_puzzle(uncurry_puzzle(spend.puzzle_reveal.to_program()))
            if args is not None:
                _, _, inner_puzzle = args
                puzzle_hash = inner_puzzle.get_tree_hash()
                private = await self.wallet_state_manager.get_private_key(puzzle_hash)
                synthetic_secret_key = calculate_synthetic_secret_key(private, DEFAULT_HIDDEN_PUZZLE_HASH)
                conditions = conditions_dict_for_solution(
                    spend.puzzle_reveal.to_program(),
                    spend.solution.to_program(),
                    self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
                )
                synthetic_pk = synthetic_secret_key.get_g1()
                for pk, msg in pkm_pairs_for_conditions_dict(
                    conditions, spend.coin.name(), self.wallet_state_manager.constants.AGG_SIG_ME_ADDITIONAL_DATA
                ):
                    try:
                        assert bytes(synthetic_pk) == pk
                        sigs.append(AugSchemeMPL.sign(synthetic_secret_key, msg))
                    except AssertionError:
                        raise ValueError("This spend bundle cannot be signed by the CAT wallet")

        agg_sig = AugSchemeMPL.aggregate(sigs)
        return SpendBundle.aggregate([spend_bundle, SpendBundle([], agg_sig)])

    async def inner_puzzle_for_cat_puzhash(self, cat_hash: bytes32) -> Program:
        record: Optional[
            DerivationRecord
        ] = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(cat_hash)
        if record is None:
            raise RuntimeError(f"Missing Derivation Record for CAT puzzle_hash {cat_hash}")
        inner_puzzle: Program = self.standard_wallet.puzzle_for_pk(record.pubkey)
        return inner_puzzle

    async def convert_puzzle_hash(self, puzzle_hash: bytes32) -> bytes32:
        record: Optional[
            DerivationRecord
        ] = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(puzzle_hash)
        if record is None:
            return puzzle_hash  # TODO: check if we have a test for this case!
        else:
            return (await self.inner_puzzle_for_cat_puzhash(puzzle_hash)).get_tree_hash()

    async def get_lineage_proof_for_coin(self, coin: Coin) -> Optional[LineageProof]:
        return await self.lineage_store.get_lineage_proof(coin.parent_coin_info)

    async def create_tandem_xch_tx(
        self,
        fee: uint64,
        amount_to_claim: uint64,
        announcement_to_assert: Optional[Announcement] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        exclude_coin_amounts: Optional[List[uint64]] = None,
        reuse_puzhash: Optional[bool] = None,
    ) -> Tuple[TransactionRecord, Optional[Announcement]]:
        """
        This function creates a non-CAT transaction to pay fees, contribute funds for issuance, and absorb melt value.
        It is meant to be called in `generate_unsigned_spendbundle` and as such should be called under the
        wallet_state_manager lock
        """
        announcement = None
        if reuse_puzhash is None:
            reuse_puzhash_config = self.wallet_state_manager.config.get("reuse_public_key_for_change", None)
            if reuse_puzhash_config is None:
                reuse_puzhash = False
            else:
                reuse_puzhash = reuse_puzhash_config.get(
                    str(self.wallet_state_manager.wallet_node.logged_in_fingerprint), False
                )
        if fee > amount_to_claim:
            chia_coins = await self.standard_wallet.select_coins(
                fee,
                min_coin_amount=min_coin_amount,
                max_coin_amount=max_coin_amount,
                excluded_coin_amounts=exclude_coin_amounts,
            )
            origin_id = list(chia_coins)[0].name()
            chia_tx = await self.standard_wallet.generate_signed_transaction(
                uint64(0),
                (await self.standard_wallet.get_puzzle_hash(not reuse_puzhash)),
                fee=uint64(fee - amount_to_claim),
                coins=chia_coins,
                origin_id=origin_id,  # We specify this so that we know the coin that is making the announcement
                negative_change_allowed=False,
                coin_announcements_to_consume={announcement_to_assert} if announcement_to_assert is not None else None,
                reuse_puzhash=reuse_puzhash,
            )
            assert chia_tx.spend_bundle is not None

            message = None
            for spend in chia_tx.spend_bundle.coin_spends:
                if spend.coin.name() == origin_id:
                    conditions = spend.puzzle_reveal.to_program().run(spend.solution.to_program()).as_python()
                    for condition in conditions:
                        if condition[0] == ConditionOpcode.CREATE_COIN_ANNOUNCEMENT:
                            message = condition[1]

            assert message is not None
            announcement = Announcement(origin_id, message)
        else:
            chia_coins = await self.standard_wallet.select_coins(
                fee,
                min_coin_amount=min_coin_amount,
                max_coin_amount=max_coin_amount,
                excluded_coin_amounts=exclude_coin_amounts,
            )
            selected_amount = sum([c.amount for c in chia_coins])
            chia_tx = await self.standard_wallet.generate_signed_transaction(
                uint64(selected_amount + amount_to_claim - fee),
                (await self.standard_wallet.get_puzzle_hash(not reuse_puzhash)),
                coins=chia_coins,
                negative_change_allowed=True,
                coin_announcements_to_consume={announcement_to_assert} if announcement_to_assert is not None else None,
                reuse_puzhash=reuse_puzhash,
            )
            assert chia_tx.spend_bundle is not None

        return chia_tx, announcement

    async def generate_unsigned_spendbundle(
        self,
        payments: List[Payment],
        fee: uint64 = uint64(0),
        cat_discrepancy: Optional[Tuple[int, Program, Program]] = None,  # (extra_delta, tail_reveal, tail_solution)
        coins: Optional[Set[Coin]] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        exclude_coin_amounts: Optional[List[uint64]] = None,
        exclude_coins: Optional[Set[Coin]] = None,
        reuse_puzhash: Optional[bool] = None,
    ) -> Tuple[SpendBundle, Optional[TransactionRecord]]:
        if coin_announcements_to_consume is not None:
            coin_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in coin_announcements_to_consume}
        else:
            coin_announcements_bytes = None

        if puzzle_announcements_to_consume is not None:
            puzzle_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in puzzle_announcements_to_consume}
        else:
            puzzle_announcements_bytes = None

        if cat_discrepancy is not None:
            extra_delta, tail_reveal, tail_solution = cat_discrepancy
        else:
            extra_delta, tail_reveal, tail_solution = 0, Program.to([]), Program.to([])
        payment_amount: int = sum([p.amount for p in payments])
        starting_amount: int = payment_amount - extra_delta
        if reuse_puzhash is None:
            reuse_puzhash_config = self.wallet_state_manager.config.get("reuse_public_key_for_change", None)
            if reuse_puzhash_config is None:
                reuse_puzhash = False
            else:
                reuse_puzhash = reuse_puzhash_config.get(
                    str(self.wallet_state_manager.wallet_node.logged_in_fingerprint), False
                )
        if coins is None:
            if exclude_coins is None:
                exclude_coins = set()
            cat_coins = await self.select_coins(
                uint64(starting_amount),
                exclude=list(exclude_coins),
                min_coin_amount=min_coin_amount,
                max_coin_amount=max_coin_amount,
                excluded_coin_amounts=exclude_coin_amounts,
            )
        elif exclude_coins is not None:
            raise ValueError("Can't exclude coins when also specifically including coins")
        else:
            cat_coins = coins

        selected_cat_amount = sum([c.amount for c in cat_coins])
        assert selected_cat_amount >= starting_amount

        # Figure out if we need to absorb/melt some XCH as part of this
        regular_chia_to_claim: int = 0
        if payment_amount > starting_amount:
            fee = uint64(fee + payment_amount - starting_amount)
        elif payment_amount < starting_amount:
            regular_chia_to_claim = payment_amount

        need_chia_transaction = (fee > 0 or regular_chia_to_claim > 0) and (fee - regular_chia_to_claim != 0)

        # Calculate standard puzzle solutions
        change = selected_cat_amount - starting_amount
        primaries = payments.copy()

        if change > 0:
            derivation_record = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
                list(cat_coins)[0].puzzle_hash
            )
            if derivation_record is not None and reuse_puzhash:
                change_puzhash = self.standard_wallet.puzzle_hash_for_pk(derivation_record.pubkey)
                for payment in payments:
                    if change_puzhash == payment.puzzle_hash and change == payment.amount:
                        # We cannot create two coins has same id, create a new puzhash for the change
                        change_puzhash = await self.get_new_inner_hash()
                        break
            else:
                change_puzhash = await self.get_new_inner_hash()
            primaries.append(Payment(change_puzhash, uint64(change), [change_puzhash]))

        # Loop through the coins we've selected and gather the information we need to spend them
        spendable_cat_list = []
        chia_tx = None
        first = True
        announcement: Announcement
        for coin in cat_coins:
            if first:
                first = False
                announcement = Announcement(coin.name(), std_hash(b"".join([c.name() for c in cat_coins])))
                if need_chia_transaction:
                    if fee > regular_chia_to_claim:
                        chia_tx, _ = await self.create_tandem_xch_tx(
                            fee,
                            uint64(regular_chia_to_claim),
                            announcement_to_assert=announcement,
                            min_coin_amount=min_coin_amount,
                            max_coin_amount=max_coin_amount,
                            exclude_coin_amounts=exclude_coin_amounts,
                            reuse_puzhash=reuse_puzhash,
                        )
                        innersol = self.standard_wallet.make_solution(
                            primaries=primaries,
                            coin_announcements={announcement.message},
                            coin_announcements_to_assert=coin_announcements_bytes,
                            puzzle_announcements_to_assert=puzzle_announcements_bytes,
                        )
                    elif regular_chia_to_claim > fee:
                        chia_tx, _ = await self.create_tandem_xch_tx(
                            fee,
                            uint64(regular_chia_to_claim),
                            min_coin_amount=min_coin_amount,
                            max_coin_amount=max_coin_amount,
                            exclude_coin_amounts=exclude_coin_amounts,
                            reuse_puzhash=reuse_puzhash,
                        )
                        innersol = self.standard_wallet.make_solution(
                            primaries=primaries,
                            coin_announcements={announcement.message},
                            coin_announcements_to_assert={announcement.name()},
                        )
                else:
                    innersol = self.standard_wallet.make_solution(
                        primaries=primaries,
                        coin_announcements={announcement.message},
                        coin_announcements_to_assert=coin_announcements_bytes,
                        puzzle_announcements_to_assert=puzzle_announcements_bytes,
                    )
            else:
                innersol = self.standard_wallet.make_solution(
                    primaries=[],
                    coin_announcements_to_assert={announcement.name()},
                )
            if cat_discrepancy is not None:
                # TODO: This line is a hack, make_solution should allow us to pass extra conditions to it
                innersol = Program.to(
                    [[], (1, Program.to([51, None, -113, tail_reveal, tail_solution]).cons(innersol.at("rfr"))), []]
                )
            inner_puzzle = await self.inner_puzzle_for_cat_puzhash(coin.puzzle_hash)
            lineage_proof = await self.get_lineage_proof_for_coin(coin)
            assert lineage_proof is not None
            new_spendable_cat = SpendableCAT(
                coin,
                self.cat_info.limitations_program_hash,
                inner_puzzle,
                innersol,
                limitations_solution=tail_solution,
                extra_delta=extra_delta,
                lineage_proof=lineage_proof,
                limitations_program_reveal=tail_reveal,
            )
            spendable_cat_list.append(new_spendable_cat)

        cat_spend_bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendable_cat_list)
        chia_spend_bundle = SpendBundle([], G2Element())
        if chia_tx is not None and chia_tx.spend_bundle is not None:
            chia_spend_bundle = chia_tx.spend_bundle

        return (
            SpendBundle.aggregate(
                [
                    cat_spend_bundle,
                    chia_spend_bundle,
                ]
            ),
            chia_tx,
        )

    async def generate_signed_transaction(
        self,
        amounts: List[uint64],
        puzzle_hashes: List[bytes32],
        fee: uint64 = uint64(0),
        coins: Optional[Set[Coin]] = None,
        ignore_max_send_amount: bool = False,
        memos: Optional[List[List[bytes]]] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        exclude_coin_amounts: Optional[List[uint64]] = None,
        exclude_cat_coins: Optional[Set[Coin]] = None,
        cat_discrepancy: Optional[Tuple[int, Program, Program]] = None,  # (extra_delta, tail_reveal, tail_solution)
        reuse_puzhash: Optional[bool] = None,
    ) -> List[TransactionRecord]:
        if memos is None:
            memos = [[] for _ in range(len(puzzle_hashes))]

        if not (len(memos) == len(puzzle_hashes) == len(amounts)):
            raise ValueError("Memos, puzzle_hashes, and amounts must have the same length")

        payments = []
        for amount, puzhash, memo_list in zip(amounts, puzzle_hashes, memos):
            memos_with_hint: List[bytes] = [puzhash]
            memos_with_hint.extend(memo_list)
            payments.append(Payment(puzhash, amount, memos_with_hint))

        payment_sum = sum([p.amount for p in payments])
        if not ignore_max_send_amount:
            max_send = await self.get_max_send_amount()
            if payment_sum > max_send:
                raise ValueError(f"Can't send more than {max_send} mojos in a single transaction")
        unsigned_spend_bundle, chia_tx = await self.generate_unsigned_spendbundle(
            payments,
            fee,
            cat_discrepancy=cat_discrepancy,  # (extra_delta, tail_reveal, tail_solution)
            coins=coins,
            coin_announcements_to_consume=coin_announcements_to_consume,
            puzzle_announcements_to_consume=puzzle_announcements_to_consume,
            min_coin_amount=min_coin_amount,
            max_coin_amount=max_coin_amount,
            exclude_coin_amounts=exclude_coin_amounts,
            exclude_coins=exclude_cat_coins,
            reuse_puzhash=reuse_puzhash,
        )
        spend_bundle = await self.sign(unsigned_spend_bundle)
        # TODO add support for array in stored records
        tx_list = [
            TransactionRecord(
                confirmed_at_height=uint32(0),
                created_at_time=uint64(int(time.time())),
                to_puzzle_hash=puzzle_hashes[0],
                amount=uint64(payment_sum),
                fee_amount=fee,
                confirmed=False,
                sent=uint32(0),
                spend_bundle=spend_bundle,
                additions=spend_bundle.additions(),
                removals=spend_bundle.removals(),
                wallet_id=self.id(),
                sent_to=[],
                trade_id=None,
                type=uint32(TransactionType.OUTGOING_TX.value),
                name=spend_bundle.name(),
                memos=list(compute_memos(spend_bundle).items()),
            )
        ]

        if chia_tx is not None:
            tx_list.append(
                TransactionRecord(
                    confirmed_at_height=chia_tx.confirmed_at_height,
                    created_at_time=chia_tx.created_at_time,
                    to_puzzle_hash=chia_tx.to_puzzle_hash,
                    amount=chia_tx.amount,
                    fee_amount=chia_tx.fee_amount,
                    confirmed=chia_tx.confirmed,
                    sent=chia_tx.sent,
                    spend_bundle=None,
                    additions=chia_tx.additions,
                    removals=chia_tx.removals,
                    wallet_id=chia_tx.wallet_id,
                    sent_to=chia_tx.sent_to,
                    trade_id=chia_tx.trade_id,
                    type=chia_tx.type,
                    name=chia_tx.name,
                    memos=[],
                )
            )

        return tx_list

    async def add_lineage(self, name: bytes32, lineage: Optional[LineageProof]) -> None:
        """
        Lineage proofs are stored as a list of parent coins and the lineage proof you will need if they are the
        parent of the coin you are trying to spend. 'If I'm your parent, here's the info you need to spend yourself'
        """
        self.log.info(f"Adding parent {name.hex()}: {lineage}")
        if lineage is not None:
            await self.lineage_store.add_lineage_proof(name, lineage)

    async def remove_lineage(self, name: bytes32) -> None:
        self.log.info(f"Removing parent {name} (probably had a non-CAT parent)")
        await self.lineage_store.remove_lineage_proof(name)

    async def save_info(self, cat_info: CATInfo) -> None:
        self.cat_info = cat_info
        current_info = self.wallet_info
        data_str = bytes(cat_info).hex()
        wallet_info = WalletInfo(current_info.id, current_info.name, current_info.type, data_str)
        self.wallet_info = wallet_info
        await self.wallet_state_manager.user_store.update_wallet(wallet_info)

    async def match_puzzle_info(self, puzzle_driver: PuzzleInfo) -> bool:
        return (
            AssetType(puzzle_driver.type()) == AssetType.CAT
            and puzzle_driver["tail"] == bytes.fromhex(self.get_asset_id())
            and puzzle_driver.also() is None
        )

    async def get_puzzle_info(self, asset_id: bytes32) -> PuzzleInfo:
        return PuzzleInfo({"type": AssetType.CAT.value, "tail": "0x" + self.get_asset_id()})

    async def get_coins_to_offer(
        self,
        asset_id: Optional[bytes32],
        amount: uint64,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
    ) -> Set[Coin]:
        balance = await self.get_confirmed_balance()
        if balance < amount:
            raise Exception(f"insufficient funds in wallet {self.id()}")
        return await self.select_coins(amount, min_coin_amount=min_coin_amount, max_coin_amount=max_coin_amount)

    ########################
    # OuterWallet Protocol #
    ########################
    @staticmethod
    async def select_coins_from_spend_descriptions(
        wallet_state_manager: Any, coin_spec: Solver, previous_actions: List[CoinSpend]
    ) -> Tuple[List[SpendDescription], Optional[Solver]]:
        target_amount: int = cast_to_int(coin_spec["amount"])
        expected_tail: bytes32
        if "asset_id" in coin_spec:
            expected_tail = bytes32(coin_spec["asset_id"])
        elif "asset_description" in coin_spec:
            expected_tail = bytes32(coin_spec["asset_description"]["tail"])
        elif "asset_types" in coin_spec:
            expected_tail = bytes32(coin_spec["asset_types"][0]["committed_args"].at("rf").as_python())
        else:
            return [], coin_spec

        # First, get all of the spends that create coins that are not also consumed in this bundle
        non_ephemeral_parents: List[bytes32] = [
            coin.parent_coin_info for coin in SpendBundle(previous_actions, G2Element()).not_ephemeral_additions()
        ]
        parent_spends: List[CoinSpend] = [cs for cs in previous_actions if cs.coin.name() in non_ephemeral_parents]

        # Loop through spends looking for spends of this type of CAT
        selected_coins: List[SpendDescription] = []
        for parent_spend in parent_spends:
            curried_args = match_cat_puzzle(uncurry_puzzle(parent_spend.puzzle_reveal.to_program()))
            if curried_args is not None:
                mod_hash, genesis_coin_checker_hash, inner_puzzle = curried_args
                if genesis_coin_checker_hash.as_python() == expected_tail:
                    # Once we've found one, start adding new coins to selected_coins until we reach the target amount
                    parent_lineage = LineageProof(
                        parent_spend.coin.parent_coin_info,
                        inner_puzzle.get_tree_hash(),
                        uint64(parent_spend.coin.amount),
                    )
                    inner_solution: Program = parent_spend.solution.to_program().at("f")
                    for condition in inner_puzzle.run(inner_solution).as_iter():
                        if condition.first() == 51:
                            inner_puzzle_hash = bytes32(condition.at("rf").as_python())
                            amount: uint64 = uint64(condition.at("rrf").as_int())
                            selected_coin: Coin = Coin(parent_spend.coin.name(), inner_puzzle_hash, amount)
                            selected_coins.append(
                                SpendDescription(
                                    selected_coin,
                                    PuzzleDescription(
                                        OuterDriver(expected_tail),
                                        Solver({"tail": "0x" + expected_tail.hex()}),
                                    ),
                                    SolutionDescription(
                                        [],
                                        Solver(
                                            {
                                                "my_id": "0x" + selected_coin.name().hex(),
                                                "lineage_proof": disassemble(parent_lineage.to_program()),
                                            }
                                        ),
                                    ),
                                    *(
                                        await wallet_state_manager.get_inner_descriptions_for_puzzle_hash(
                                            inner_puzzle_hash
                                        )
                                    ),
                                )
                            )
                            if sum(c.coin.amount for c in selected_coins) >= target_amount:
                                break

            if sum(c.coin.amount for c in selected_coins) >= target_amount and len(selected_coins) > 0:
                break

        # Need to select_new_coins for any remaining balance
        remaining_balance: int = target_amount - sum(c.coin.amount for c in selected_coins)
        return (
            selected_coins,
            Solver({"asset_id": "0x" + expected_tail.hex(), "amount": str(remaining_balance)})
            if remaining_balance > 0
            else None,
        )

    @staticmethod
    async def select_new_coins(
        wallet_state_manager: Any, coin_spec: Solver, exclude: List[Coin] = []
    ) -> List[SpendDescription]:
        target_amount: int = cast_to_int(coin_spec["amount"])
        expected_tail: bytes32
        if "asset_id" in coin_spec:
            expected_tail = bytes32(coin_spec["asset_id"])
        elif "asset_description" in coin_spec:
            expected_tail = bytes32(coin_spec["asset_description"]["tail"])
        elif "asset_types" in coin_spec:
            expected_tail = bytes32(coin_spec["asset_types"][0]["committed_args"].at("rf").as_python())
        else:
            return []

        wallet = await wallet_state_manager.get_wallet_for_asset_id(expected_tail.hex())

        additional_coins: Set[Coin] = await wallet.select_coins(
            target_amount,
            exclude=exclude,
        )
        return [
            SpendDescription(
                coin,
                PuzzleDescription(
                    OuterDriver(expected_tail),
                    Solver({"tail": "0x" + expected_tail.hex()}),
                ),
                SolutionDescription(
                    [],
                    Solver(
                        {
                            "my_id": "0x" + coin.name().hex(),
                            "lineage_proof": disassemble((await wallet.get_lineage_proof_for_coin(coin)).to_program()),
                        }
                    ),
                ),
                *(
                    await wallet_state_manager.get_inner_descriptions_for_puzzle_hash(
                        (await wallet.inner_puzzle_for_cat_puzhash(coin.puzzle_hash)).get_tree_hash()
                    )
                ),
            )
            for coin in additional_coins
        ]


@dataclasses.dataclass(frozen=True)
class OuterDriver:
    tail: bytes32

    # TODO: This is not great, we should move the coin selection logic in here
    @staticmethod
    def get_wallet_class() -> Type[WalletProtocol]:
        return CATWallet

    def get_actions(self) -> Dict[str, Type[WalletAction]]:
        return {}

    def get_aliases(self) -> Dict[str, Type[ActionAlias]]:
        return {}  # TODO: RunTail or something should be here

    def construct_outer_puzzle(self, inner_puzzle: Program) -> Program:
        return construct_cat_puzzle(CAT_MOD, self.tail, inner_puzzle)

    def construct_outer_solution(
        self,
        actions: List[WalletAction],
        inner_solution: Program,
        global_environment: Solver,
        local_environment: Solver,
        optimize: bool = False,
    ) -> Program:
        # We only do all the ring logic when we're ready to push to chain
        if optimize and "spends" in global_environment:
            all_spends: List[CoinSpend] = [
                CoinSpend(
                    Coin(
                        spend["coin"]["parent_coin_info"],
                        spend["coin"]["puzzle_hash"],
                        cast_to_int(spend["coin"]["amount"]),
                    ),
                    spend["puzzle_reveal"],
                    spend["solution"],
                )
                for spend in global_environment["spends"]
            ]

            # Sort the spends deterministically so every CAT knows its role in the ring
            all_spends.sort(key=lambda cs: cs.coin.name())
            matched_spends: List[Optional[Iterator[Program]]] = [
                match_cat_puzzle(uncurry_puzzle(cs.puzzle_reveal.to_program())) for cs in all_spends
            ]
            only_same_cat_spends: List[CoinSpend] = [
                cs
                for cs, args in zip(all_spends, matched_spends)
                if args is not None and list(args)[1].as_python() == self.tail
            ]
            relevant_ids: List[bytes32] = [cs.coin.name() for cs in only_same_cat_spends]
            relevant_descriptions: List[Solver] = [
                description
                for description in global_environment["spend_descriptions"]
                if description["id"] in relevant_ids
            ]
            relevant_descriptions.sort(key=lambda description: bytes32(description["id"]))

            my_index: int = next(
                i for i, cs in enumerate(only_same_cat_spends) if cs.coin.name() == local_environment["my_id"]
            )
            previous_index: int = my_index - 1
            next_index: int = 0 if my_index == len(only_same_cat_spends) - 1 else my_index + 1

            # Get the accounting information
            consumed_coins: List[Coin] = [cs.coin for cs in only_same_cat_spends]
            output_amounts: List[int] = []
            for description in relevant_descriptions:
                for action in description["inner"]["actions"]:
                    if action["type"] == [Condition.name(), DirectPayment.name(), OfferedAmount.name()]:
                        if "amount" in action:
                            output_amounts.append(cast_to_int(action["amount"]))
                        elif "payment" in action:
                            output_amounts.append(cast_to_int(action["payment"]["amount"]))
                        else:
                            condition: Program = Condition.from_solver(action).condition
                            if condition.first().as_int() == 51 and condition.at("rrf") > 0:
                                output_amounts.append(condition.at("rrf").as_int())

            subtotals: List[int] = []
            for i, coin in enumerate(consumed_coins):
                if i == 0:
                    subtotals.append(0)
                else:
                    subtotals.append(subtotals[i - 1] + output_amounts[i] - coin.amount)

            full_solution: Program = Program.to(
                [
                    inner_solution,
                    local_environment["lineage_proof"],
                    consumed_coins[previous_index].name(),
                    [
                        consumed_coins[my_index].parent_coin_info,
                        consumed_coins[my_index].puzzle_hash,
                        consumed_coins[my_index].amount,
                    ],
                    [
                        consumed_coins[next_index].parent_coin_info,
                        list(only_same_cat_spends[next_index].puzzle_reveal.to_program().uncurry()[1].as_iter())[
                            2
                        ].get_tree_hash(),
                        consumed_coins[next_index].amount,
                    ],
                    subtotals[my_index],
                    0,  # TODO: this could be a thing
                ]
            )
            return full_solution
        else:
            # We use a placeholder in the non-optimal case to leave just enough information to infer the driver later
            placeholder: Program = Program.to(
                [inner_solution, local_environment["lineage_proof"], None, local_environment["my_id"], None, None, None]
            )
            return placeholder

    def check_and_modify_actions(
        self,
        outer_actions: List[WalletAction],
        inner_actions: List[WalletAction],
    ) -> Tuple[List[WalletAction], List[WalletAction]]:
        return outer_actions, inner_actions

    @classmethod
    def match_puzzle(
        cls, puzzle: Program, mod: Program, curried_args: Program
    ) -> Optional[Tuple[PuzzleDescription, Program]]:
        args = match_cat_puzzle(UncurriedPuzzle(mod, curried_args))
        if args is not None:
            mod_hash, genesis_coin_checker_hash, inner_puzzle = args
            tail: bytes32 = bytes32(genesis_coin_checker_hash.as_python())
            return (
                PuzzleDescription(
                    cls(tail),
                    Solver(
                        {
                            "tail": "0x" + tail.hex(),
                            "asset_types": cls.get_asset_types(Solver({"tail": "0x" + tail.hex()})),
                        }
                    ),
                ),
                inner_puzzle,
            )

        return None

    @classmethod
    def match_solution(cls, solution: Program) -> Optional[Tuple[SolutionDescription, Program]]:
        this_coin_info: Program = solution.at("rrrf")
        if this_coin_info.atom is None:
            parent_id, puzzle_hash, amount = this_coin_info.as_iter()
            my_id: bytes32 = Coin(
                bytes32(parent_id.as_python()), bytes32(puzzle_hash.as_python()), uint64(amount.as_int())
            ).name()
        else:
            my_id = bytes32(this_coin_info.as_python())

        return (
            SolutionDescription(
                [],
                Solver(
                    {
                        "lineage_proof": disassemble(solution.at("rf")),
                        "my_id": "0x" + my_id.hex(),
                    }
                ),
            ),
            solution.first(),
        )

    @staticmethod
    def get_asset_types(request: Solver) -> List[Solver]:
        return [
            Solver(
                # solution template: (CAT_MOD_HASH TAIL_HASH INNER_PUZZLE . rest_of_solution)
                {
                    "mod": disassemble(CAT_MOD),
                    "solution_template": f"(1 {'1' if 'tail' in request else '-1'} 0 . $)",
                    "committed_args": (
                        f"({'0x' + CAT_MOD_HASH.hex()} {'0x' + request['tail'].hex() if 'tail' in request else '()'}"
                        " () . ())"
                    ),
                }
            )
        ]

    @staticmethod
    def match_asset_types(asset_types: List[Solver]) -> bool:
        if len(asset_types) == 1 and asset_types[0]["mod"] == CAT_MOD:
            return True
        return False

    def get_required_signatures(self, solution_description: SolutionDescription) -> List[Tuple[G1Element, bytes, bool]]:
        return []


if TYPE_CHECKING:
    from chia.wallet.wallet_protocol import WalletProtocol

    _dummy: WalletProtocol = CATWallet()
