from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pytest

from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.simulator.setup_nodes import SimulatorsAndWallets, SimulatorsAndWalletsServices
from chia.simulator.simulator_protocol import FarmNewBlockProtocol, ReorgProtocol
from chia.simulator.time_out_assert import adjusted_timeout, time_out_assert, time_out_assert_not_none
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.peer_info import PeerInfo
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import encode_puzzle_hash
from chia.util.ints import uint16, uint32, uint64, uint128
from chia.wallet.cat_wallet.dao_cat_wallet import DAOCATWallet
from chia.wallet.dao_wallet.dao_info import DAORules
from chia.wallet.dao_wallet.dao_wallet import DAOWallet
from chia.wallet.transaction_record import TransactionRecord
from tests.util.rpc import validate_get_routes


async def get_proposal_state(wallet: DAOWallet, index: int) -> Tuple[Optional[bool], Optional[bool]]:
    return wallet.dao_info.proposals_list[index].passed, wallet.dao_info.proposals_list[index].closed


async def rpc_state(
    timeout: float,
    async_function: Callable[[Any], Any],
    params: List[Dict[str, Any]],
    condition_func: Callable[[Dict[str, Any]], Any],
    result: Optional[Any] = None,
) -> Union[bool, Dict[str, Any]]:
    __tracebackhide__ = True

    timeout = adjusted_timeout(timeout=timeout)

    start = time.monotonic()

    while True:
        resp = await async_function(*params)
        assert isinstance(resp, dict)
        try:
            if result:
                if condition_func(resp) == result:
                    return True
            else:
                if condition_func(resp):
                    return resp
        except IndexError:
            continue

        now = time.monotonic()
        elapsed = now - start
        if elapsed >= timeout:
            raise asyncio.TimeoutError(
                f"timed out while waiting for {async_function.__name__}(): {elapsed} >= {timeout}",
            )

        await asyncio.sleep(0.3)


puzzle_hash_0 = bytes32(32 * b"0")


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_creation(self_hostname: str, three_wallet_nodes: SimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 1
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    ph = await wallet.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    cat_amt = 2000
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),  # 10%
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=uint64(1),
    )

    # Try to create a DAO with more CATs than xch balance
    with pytest.raises(ValueError) as e_info:
        async with wallet_node_0.wallet_state_manager.lock:
            dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
                wallet_node_0.wallet_state_manager,
                wallet,
                uint64(funds + 1),
                dao_rules,
            )
    assert e_info.value.args[0] == f"Your balance of {funds} mojos is not enough to create {funds + 1} CATs"

    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet,
            uint64(cat_amt * 2),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Check the spend was successful
    treasury_id = dao_wallet_0.dao_info.treasury_id

    # check the dao wallet balances
    await time_out_assert(20, dao_wallet_0.get_confirmed_balance, uint128(1))
    await time_out_assert(20, dao_wallet_0.get_unconfirmed_balance, uint128(1))
    await time_out_assert(20, dao_wallet_0.get_pending_change_balance, uint64(0))
    await time_out_assert(20, dao_wallet_0.get_spendable_balance, uint128(1))

    # check select coins
    no_coins = await dao_wallet_0.select_coins(uint64(2))
    assert no_coins == set()
    selected_coins = await dao_wallet_0.select_coins(uint64(1))
    assert len(selected_coins) == 1

    # get the cat wallets
    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    dao_cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.dao_cat_wallet_id]
    cat_wallet_0_bal = await cat_wallet_0.get_confirmed_balance()
    assert cat_wallet_0_bal == cat_amt * 2

    # Create the other user's wallet from the treasury id
    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
            wallet_node_1.wallet_state_manager,
            wallet_1,
            treasury_id,
        )
    assert dao_wallet_1 is not None
    assert dao_wallet_0.dao_info.treasury_id == dao_wallet_1.dao_info.treasury_id

    # Get the cat wallets for wallet_1
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    assert cat_wallet_1

    # Send some cats to the dao_cat lockup
    dao_cat_amt = uint64(100)
    async with wallet_node_0.wallet_state_manager.lock:
        txs, new_coins = await dao_wallet_0.create_new_dao_cats(dao_cat_amt, push=True)
    sb = txs[0].spend_bundle
    assert isinstance(sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, sb.name())
    await full_node_api.process_transaction_records(records=txs)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Test that we can get spendable coins from both cat and dao_cat wallet
    fake_proposal_id = Program.to("proposal_id").get_tree_hash()
    spendable_coins = await dao_cat_wallet_0.wallet_state_manager.get_spendable_coins_for_wallet(
        dao_cat_wallet_0.id(), None
    )

    assert len(spendable_coins) > 0
    coins = await dao_cat_wallet_0.advanced_select_coins(1, fake_proposal_id)
    assert len(coins) > 0
    # check that we have selected the coin from dao_cat_wallet
    assert list(coins)[0].coin.amount == dao_cat_amt

    # send some cats from wallet_0 to wallet_1 so we can test voting
    cat_txs = await cat_wallet_0.generate_signed_transaction([cat_amt], [ph_1])
    await wallet.wallet_state_manager.add_pending_transaction(cat_txs[0])
    sb = cat_txs[0].spend_bundle
    assert isinstance(sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, sb.name())
    await full_node_api.process_transaction_records(records=cat_txs)

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await time_out_assert(10, cat_wallet_1.get_confirmed_balance, cat_amt)

    # Smaller tests of dao_wallet funcs for coverage
    await dao_wallet_0.adjust_filter_level(uint64(10))
    assert dao_wallet_0.dao_info.filter_below_vote_amount == uint64(10)

    await dao_wallet_0.set_name("Renamed Wallet")
    assert dao_wallet_0.get_name() == "Renamed Wallet"

    new_inner_puzhash = await dao_wallet_0.get_new_p2_inner_hash()
    assert isinstance(new_inner_puzhash, bytes32)


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_funding(self_hostname: str, three_wallet_nodes: SimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 1
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_node_2, server_2 = wallets[2]
    wallet = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_1.wallet_state_manager.main_wallet
    ph = await wallet.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()
    ph_2 = await wallet_2.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_2.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_2.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_2))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    cat_amt = 300000
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),  # 10%
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=uint64(1),
    )

    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet,
            uint64(cat_amt),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    treasury_id = dao_wallet_0.dao_info.treasury_id

    # Get the full node sim to process the wallet creation spend
    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # get the cat wallets
    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    await time_out_assert(20, cat_wallet_0.get_confirmed_balance, cat_amt)

    # Create funding spends for xch and cat
    xch_funds = uint64(500000)
    cat_funds = uint64(100000)
    funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(xch_funds)
    funding_sb = funding_tx.spend_bundle
    assert isinstance(funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, funding_sb.name())
    await full_node_api.process_transaction_records(records=[funding_tx])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Check that the funding spend is recognized by both dao wallets
    await time_out_assert(20, dao_wallet_0.get_balance_by_asset_type, xch_funds)

    cat_funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(
        cat_funds, funding_wallet_id=cat_wallet_0.id()
    )
    cat_funding_sb = cat_funding_tx.spend_bundle
    assert isinstance(cat_funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_funding_sb.name())
    await full_node_api.process_transaction_records(records=[cat_funding_tx])

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    await time_out_assert(20, cat_wallet_0.get_confirmed_balance, cat_amt - cat_funds)

    # Check that the funding spend is found
    cat_id = bytes32.from_hexstr(cat_wallet_0.get_asset_id())
    await time_out_assert(20, dao_wallet_0.get_balance_by_asset_type, cat_funds, cat_id)

    # Create the other user's wallet from the treasury id
    dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_1.wallet_state_manager,
        wallet_1,
        treasury_id,
    )
    assert dao_wallet_1 is not None
    assert dao_wallet_1.dao_info.treasury_id == dao_wallet_1.dao_info.treasury_id
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Get the cat wallets for wallet_1
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    assert cat_wallet_1
    assert cat_wallet_1.cat_info.limitations_program_hash == cat_id

    await time_out_assert(30, dao_wallet_1.get_balance_by_asset_type, xch_funds)
    await time_out_assert(30, dao_wallet_1.get_balance_by_asset_type, cat_funds, cat_id)

    assert dao_wallet_0.dao_info.assets == [None, cat_id]
    assert dao_wallet_1.dao_info.assets == [None, cat_id]


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_proposals(self_hostname: str, three_wallet_nodes: SimulatorsAndWallets, trusted: bool) -> None:
    """
    Test a set of proposals covering:
    - the spend, update, and mint types.
    - passing and failing
    - force closing broken proposals

    total cats issued: 300k
    each wallet holds: 100k

    The proposal types and amounts voted are:
    P0 Spend => Pass
    P1 Mint => Pass
    P2 Update => Pass
    P3 Spend => Fail
    P4 Bad Spend => Force Close

    """
    num_blocks = 1
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_node_2, server_2 = wallets[2]
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_2.wallet_state_manager.main_wallet
    ph_0 = await wallet_0.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()
    ph_2 = await wallet_2.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_2.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_2.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_0))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_2))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet_0.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    # set a standard fee amount to use in all txns
    base_fee = uint64(100)

    # set the cat issuance and DAO rules
    cat_issuance = 300000
    proposal_min_amt = uint64(101)
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(190000),
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=proposal_min_amt,
    )

    # Create the DAO.
    # This takes two steps: create the treasury singleton, wait for oracle_spend_delay and
    # then complete the eve spend
    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet_0,
            uint64(cat_issuance),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    dao_cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.dao_cat_wallet_id]
    await time_out_assert(10, cat_wallet_0.get_confirmed_balance, cat_issuance)
    assert dao_cat_wallet_0

    treasury_id = dao_wallet_0.dao_info.treasury_id

    # Create dao_wallet_1 from the treasury id
    dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_1.wallet_state_manager,
        wallet_1,
        treasury_id,
    )
    assert dao_wallet_1 is not None
    assert dao_wallet_1.dao_info.treasury_id == treasury_id
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    dao_cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.dao_cat_wallet_id]
    assert cat_wallet_1
    assert dao_cat_wallet_1

    # Create dao_wallet_2 from the treasury id
    dao_wallet_2 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_2.wallet_state_manager,
        wallet_2,
        treasury_id,
    )
    assert dao_wallet_2 is not None
    assert dao_wallet_2.dao_info.treasury_id == treasury_id
    cat_wallet_2 = dao_wallet_2.wallet_state_manager.wallets[dao_wallet_2.dao_info.cat_wallet_id]
    dao_cat_wallet_2 = dao_wallet_2.wallet_state_manager.wallets[dao_wallet_2.dao_info.dao_cat_wallet_id]
    assert cat_wallet_2
    assert dao_cat_wallet_2

    # Send 100k cats to wallet_1 and wallet_2
    cat_amt = uint64(100000)
    cat_tx = await cat_wallet_0.generate_signed_transaction([cat_amt, cat_amt], [ph_1, ph_2], fee=base_fee)
    cat_sb = cat_tx[0].spend_bundle
    await wallet_0.wallet_state_manager.add_pending_transaction(cat_tx[0])
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_sb.name())
    await full_node_api.process_transaction_records(records=cat_tx)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Lockup voting cats for all wallets
    dao_cat_0_bal = await dao_cat_wallet_0.get_votable_balance()
    txs_0, _ = await dao_cat_wallet_0.create_new_dao_cats(dao_cat_0_bal, True, fee=base_fee)
    dao_cat_sb_0 = txs_0[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb_0.name())
    await full_node_api.process_transaction_records(records=txs_0)

    dao_cat_1_bal = await dao_cat_wallet_1.get_votable_balance()
    txs_1, _ = await dao_cat_wallet_1.create_new_dao_cats(dao_cat_1_bal, True)
    dao_cat_sb_1 = txs_1[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb_1.name())
    await full_node_api.process_transaction_records(records=txs_1)

    dao_cat_2_bal = await dao_cat_wallet_2.get_votable_balance()
    txs_2, _ = await dao_cat_wallet_2.create_new_dao_cats(dao_cat_2_bal, True)
    dao_cat_sb_2 = txs_2[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb_2.name())
    await full_node_api.process_transaction_records(records=txs_2)

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    await time_out_assert(10, dao_cat_wallet_0.get_confirmed_balance, cat_amt)
    await time_out_assert(10, dao_cat_wallet_1.get_confirmed_balance, cat_amt)
    await time_out_assert(10, dao_cat_wallet_2.get_confirmed_balance, cat_amt)

    # Create funding spend so the treasury holds some XCH
    xch_funds = uint64(500000)
    funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(xch_funds)
    funding_sb = funding_tx.spend_bundle
    assert isinstance(funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, funding_sb.name())
    await full_node_api.process_transaction_records(records=[funding_tx])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Check that the funding spend is recognized by all wallets
    await time_out_assert(10, dao_wallet_0.get_balance_by_asset_type, xch_funds)
    await time_out_assert(10, dao_wallet_1.get_balance_by_asset_type, xch_funds)
    await time_out_assert(10, dao_wallet_2.get_balance_by_asset_type, xch_funds)

    # Create Proposals

    # Proposal 0: Spend xch to wallet_2.
    recipient_puzzle_hash = await wallet_2.get_new_puzzlehash()
    proposal_amount_1 = uint64(9998)
    xch_proposal_inner = dao_wallet_0.generate_simple_proposal_innerpuz(
        [recipient_puzzle_hash],
        [proposal_amount_1],
        [None],
    )
    proposal_tx = await dao_wallet_0.generate_new_proposal(xch_proposal_inner, dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    assert len(dao_wallet_0.dao_info.proposals_list) == 1
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal
    assert dao_wallet_0.dao_info.proposals_list[0].timer_coin is not None
    prop_0 = dao_wallet_0.dao_info.proposals_list[0]

    # Proposal 1: Mint new CATs
    new_mint_amount = uint64(1000)
    mint_proposal_inner = await dao_wallet_0.generate_mint_proposal_innerpuz(
        new_mint_amount,
        recipient_puzzle_hash,
    )

    proposal_tx = await dao_wallet_0.generate_new_proposal(mint_proposal_inner, vote_amount=dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    assert len(dao_wallet_0.dao_info.proposals_list) == 2
    prop_1 = dao_wallet_0.dao_info.proposals_list[1]

    # Proposal 2: Update DAO Rules.
    new_dao_rules = DAORules(
        proposal_timelock=uint64(8),
        soft_close_length=uint64(4),
        attendance_required=uint64(150000),
        pass_percentage=uint64(7500),
        self_destruct_length=uint64(12),
        oracle_spend_delay=uint64(5),
        proposal_minimum_amount=uint64(1),
    )
    update_inner = await dao_wallet_0.generate_update_proposal_innerpuz(new_dao_rules)
    proposal_tx = await dao_wallet_0.generate_new_proposal(update_inner, dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    assert len(dao_wallet_0.dao_info.proposals_list) == 3
    prop_2 = dao_wallet_0.dao_info.proposals_list[2]

    # Proposal 3: Spend xch to wallet_2 (this prop will close as failed)
    proposal_amount_2 = uint64(500)
    xch_proposal_inner = dao_wallet_1.generate_simple_proposal_innerpuz(
        [recipient_puzzle_hash], [proposal_amount_2], [None]
    )
    proposal_tx = await dao_wallet_0.generate_new_proposal(xch_proposal_inner, dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    assert len(dao_wallet_0.dao_info.proposals_list) == 4
    prop_3 = dao_wallet_0.dao_info.proposals_list[3]

    # Proposal 4: Create a 'bad' proposal (can't be executed, must be force-closed)
    xch_proposal_inner = Program.to(["x"])
    proposal_tx = await dao_wallet_0.generate_new_proposal(xch_proposal_inner, dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(20, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    assert len(dao_wallet_0.dao_info.proposals_list) == 5
    assert len(dao_wallet_1.dao_info.proposals_list) == 5
    assert len(dao_wallet_1.dao_info.proposals_list) == 5
    prop_4 = dao_wallet_0.dao_info.proposals_list[4]

    # Proposal 0 Voting: wallet 1 votes yes, wallet 2 votes no. Proposal Passes
    vote_sb_1 = await dao_wallet_1.generate_proposal_vote_spend(prop_0.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_1.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_1])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    vote_sb_2 = await dao_wallet_2.generate_proposal_vote_spend(prop_0.proposal_id, dao_cat_2_bal, False, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_2.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_2])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    total_votes = dao_cat_0_bal + dao_cat_1_bal + dao_cat_2_bal
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_0.dao_info.proposals_list[0].yes_votes == total_votes - dao_cat_2_bal
    assert dao_wallet_1.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].yes_votes == total_votes - dao_cat_2_bal
    assert dao_wallet_2.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_2.dao_info.proposals_list[0].yes_votes == total_votes - dao_cat_2_bal

    prop_0_state = await dao_wallet_0.get_proposal_state(prop_0.proposal_id)
    assert prop_0_state["passed"]
    assert prop_0_state["closable"]

    # Proposal 0 is closable, but soft_close_length has not passed.
    close_sb_0 = await dao_wallet_0.create_proposal_close_spend(prop_0.proposal_id)
    with pytest.raises(AssertionError) as e:
        await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_0.name())
    assert e.value.args[0] == "Timed assertion timed out"

    for _ in range(5):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Proposal 0: Close
    close_sb_0 = await dao_wallet_0.create_proposal_close_spend(prop_0.proposal_id)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_0.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb_0])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)
    await time_out_assert(20, wallet_2.get_confirmed_balance, funds + proposal_amount_1)
    await time_out_assert(
        20, dao_wallet_0.get_balance_by_asset_type, xch_funds - proposal_amount_1 + proposal_min_amt - 1
    )

    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_0, 0])
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_1, 0])
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_2, 0])

    # Proposal 1 vote and close
    vote_sb_1 = await dao_wallet_1.generate_proposal_vote_spend(prop_1.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_1.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_1])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    for _ in range(10):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    prop_1_state = await dao_wallet_0.get_proposal_state(prop_1.proposal_id)
    assert prop_1_state["passed"]
    assert prop_1_state["closable"]

    close_sb_1 = await dao_wallet_0.create_proposal_close_spend(prop_1.proposal_id, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_1.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb_1])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    await time_out_assert(20, cat_wallet_2.get_confirmed_balance, new_mint_amount)

    # Proposal 2 vote and close
    vote_sb_2 = await dao_wallet_1.generate_proposal_vote_spend(prop_2.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_2.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_2])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    for _ in range(10):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    prop_2_state = await dao_wallet_0.get_proposal_state(prop_2.proposal_id)
    assert prop_2_state["passed"]
    assert prop_2_state["closable"]

    close_sb_2 = await dao_wallet_0.create_proposal_close_spend(prop_2.proposal_id, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_2.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb_2])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    assert dao_wallet_0.dao_rules == new_dao_rules
    assert dao_wallet_1.dao_rules == new_dao_rules
    assert dao_wallet_2.dao_rules == new_dao_rules

    # Proposal 3 - Close as FAILED
    vote_sb_3 = await dao_wallet_1.generate_proposal_vote_spend(prop_3.proposal_id, dao_cat_1_bal, False, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_3.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_3])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    for _ in range(10):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    prop_3_state = await dao_wallet_1.get_proposal_state(prop_3.proposal_id)
    assert not prop_3_state["passed"]
    assert prop_3_state["closable"]

    close_sb_3 = await dao_wallet_0.create_proposal_close_spend(prop_3.proposal_id, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_3.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb_3])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    await time_out_assert(20, wallet_2.get_confirmed_balance, funds + proposal_amount_1)
    expected_balance = xch_funds - proposal_amount_1 + (3 * proposal_min_amt) - 3 - new_mint_amount
    await time_out_assert(20, dao_wallet_0.get_balance_by_asset_type, expected_balance)

    await time_out_assert(20, get_proposal_state, (False, True), *[dao_wallet_0, 3])
    await time_out_assert(20, get_proposal_state, (False, True), *[dao_wallet_1, 3])
    await time_out_assert(20, get_proposal_state, (False, True), *[dao_wallet_2, 3])

    # Proposal 4 - Self Destruct a broken proposal
    vote_sb_4 = await dao_wallet_1.generate_proposal_vote_spend(prop_4.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_4.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_4])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    for _ in range(10):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    prop_4_state = await dao_wallet_1.get_proposal_state(prop_4.proposal_id)
    assert prop_4_state["passed"]
    assert prop_4_state["closable"]

    with pytest.raises(Exception) as e_info:
        close_sb_4 = await dao_wallet_0.create_proposal_close_spend(prop_4.proposal_id, push=True)
    assert e_info.value.args[0] == "Unrecognised proposal type"

    close_sb_4 = await dao_wallet_0.create_proposal_close_spend(prop_4.proposal_id, push=True, self_destruct=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_4.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb_4])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    # expected balance is unchanged because broken props can't release their amount
    await time_out_assert(20, dao_wallet_0.get_balance_by_asset_type, expected_balance)
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_0, 4])
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_1, 4])
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_2, 4])

    # Remove Proposals from Memory and Free up locked coins
    await time_out_assert(20, len, 5, dao_wallet_0.dao_info.proposals_list)
    await dao_wallet_0.clear_finished_proposals_from_memory()
    free_sb = await dao_wallet_0.free_coins_from_finished_proposals()
    assert free_sb is not None
    await time_out_assert_not_none(20, full_node_api.full_node.mempool_manager.get_spendbundle, free_sb.name())
    await full_node_api.process_spend_bundles(bundles=[free_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    await dao_wallet_0.clear_finished_proposals_from_memory()
    await time_out_assert(20, len, 0, dao_wallet_0.dao_info.proposals_list)


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_proposal_partial_vote(
    self_hostname: str, three_wallet_nodes: SimulatorsAndWallets, trusted: bool
) -> None:
    num_blocks = 1
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_node_2, server_2 = wallets[2]
    wallet = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_2.wallet_state_manager.main_wallet
    ph = await wallet.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()
    ph_2 = await wallet_2.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_2.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_2.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_2))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    cat_amt = 300000
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),  # 10%
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=uint64(1),
    )

    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet,
            uint64(cat_amt),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    # Get the full node sim to process the wallet creation spend
    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # get the cat wallets
    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    await time_out_assert(10, cat_wallet_0.get_confirmed_balance, cat_amt)

    # get the dao_cat wallet
    dao_cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.dao_cat_wallet_id]

    treasury_id = dao_wallet_0.dao_info.treasury_id

    # Create the other user's wallet from the treasury id
    dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_1.wallet_state_manager,
        wallet_1,
        treasury_id,
    )
    assert dao_wallet_1 is not None
    assert dao_wallet_1.dao_info.treasury_id == treasury_id

    # Create funding spends for xch
    xch_funds = uint64(500000)
    funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(xch_funds)
    assert isinstance(funding_tx, TransactionRecord)
    funding_sb = funding_tx.spend_bundle
    assert isinstance(funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, funding_sb.name())
    await full_node_api.process_transaction_records(records=[funding_tx])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Check that the funding spend is recognized by both dao wallets
    await time_out_assert(10, dao_wallet_0.get_balance_by_asset_type, xch_funds)

    # Send some dao_cats to wallet_1
    # Get the cat wallets for wallet_1
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    dao_cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.dao_cat_wallet_id]
    assert cat_wallet_1
    assert dao_cat_wallet_1

    cat_tx = await cat_wallet_0.generate_signed_transaction([100000], [ph_1])
    cat_sb = cat_tx[0].spend_bundle
    await wallet.wallet_state_manager.add_pending_transaction(cat_tx[0])
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_sb.name())
    await full_node_api.process_transaction_records(records=cat_tx)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Create dao cats for voting
    dao_cat_0_bal = await dao_cat_wallet_0.get_votable_balance()
    txs, new_dao_cats = await dao_cat_wallet_0.create_new_dao_cats(dao_cat_0_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Create a mint proposal
    recipient_puzzle_hash = await cat_wallet_1.get_new_inner_hash()
    new_mint_amount = uint64(500)
    mint_proposal_inner = await dao_wallet_0.generate_mint_proposal_innerpuz(
        new_mint_amount,
        recipient_puzzle_hash,
    )

    vote_amount = dao_cat_0_bal - 10
    proposal_tx = await dao_wallet_0.generate_new_proposal(
        mint_proposal_inner, vote_amount=vote_amount, fee=uint64(1000)
    )
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Check the proposal is saved
    assert len(dao_wallet_0.dao_info.proposals_list) == 1
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == vote_amount
    assert dao_wallet_0.dao_info.proposals_list[0].timer_coin is not None

    # Check that wallet_1 also finds and saved the proposal
    assert len(dao_wallet_1.dao_info.proposals_list) == 1
    prop = dao_wallet_1.dao_info.proposals_list[0]

    # Create votable dao cats and add a new vote
    dao_cat_1_bal = await dao_cat_wallet_1.get_votable_balance()
    txs, new_dao_cats = await dao_cat_wallet_1.create_new_dao_cats(dao_cat_1_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    vote_sb = await dao_wallet_1.generate_proposal_vote_spend(prop.proposal_id, dao_cat_1_bal // 2, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb])

    for i in range(1, dao_rules.proposal_timelock + 1):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    total_votes = vote_amount + dao_cat_1_bal // 2

    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_0.dao_info.proposals_list[0].yes_votes == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].yes_votes == total_votes

    try:
        close_sb = await dao_wallet_0.create_proposal_close_spend(prop.proposal_id, fee=uint64(100), push=True)
    except Exception as e:
        print(e)

    await full_node_api.process_spend_bundles(bundles=[close_sb])
    balance = await cat_wallet_1.get_spendable_balance()

    assert close_sb is not None
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await time_out_assert(20, get_proposal_state, (True, True), dao_wallet_0, 0)
    await time_out_assert(20, get_proposal_state, (True, True), dao_wallet_1, 0)

    await time_out_assert(20, cat_wallet_1.get_spendable_balance, balance + new_mint_amount)
    # Can we spend the newly minted CATs?
    old_balance = await cat_wallet_0.get_spendable_balance()
    ph_0 = await cat_wallet_0.get_new_inner_hash()
    cat_tx = await cat_wallet_1.generate_signed_transaction([balance + new_mint_amount], [ph_0])
    cat_sb = cat_tx[0].spend_bundle
    await wallet_1.wallet_state_manager.add_pending_transaction(cat_tx[0])
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_sb.name())
    await full_node_api.process_transaction_records(records=cat_tx)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await time_out_assert(20, cat_wallet_1.get_spendable_balance, 0)
    await time_out_assert(20, cat_wallet_0.get_spendable_balance, old_balance + balance + new_mint_amount)


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_rpc_api(self_hostname: str, two_wallet_nodes: Any, trusted: Any) -> None:
    num_blocks = 2  # use 2 here so the test doesn't become flaky if things get slow
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet

    ph_0 = await wallet_0.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_0))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )

    await time_out_assert(30, wallet_0.get_unconfirmed_balance, funds)
    await time_out_assert(30, wallet_0.get_confirmed_balance, funds)
    await time_out_assert(30, wallet_node_0.wallet_state_manager.synced, True)
    api_0 = WalletRpcApi(wallet_node_0)
    api_1 = WalletRpcApi(wallet_node_1)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    cat_amt = 300000
    fee = 10000
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),  # 10%
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=uint64(1),
    )

    dao_wallet_0 = await api_0.create_new_wallet(
        dict(
            wallet_type="dao_wallet",
            name="DAO WALLET 1",
            mode="new",
            dao_rules=dao_rules,
            amount_of_cats=cat_amt,
            filter_amount=1,
            fee=fee,
        )
    )
    assert isinstance(dao_wallet_0, dict)
    assert dao_wallet_0.get("success")
    dao_wallet_0_id = dao_wallet_0["wallet_id"]
    dao_cat_wallet_0_id = dao_wallet_0["cat_wallet_id"]
    treasury_id = bytes32(dao_wallet_0["treasury_id"])
    spend_bundle_list = await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(dao_wallet_0_id)
    spend_bundle = spend_bundle_list[0].spend_bundle
    await time_out_assert_not_none(30, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    await time_out_assert(30, wallet_0.get_pending_change_balance, 0)
    expected_xch = funds - 1 - cat_amt - fee
    await time_out_assert(30, wallet_0.get_confirmed_balance, expected_xch)

    dao_wallet_1 = await api_1.create_new_wallet(
        dict(
            wallet_type="dao_wallet",
            name="DAO WALLET 2",
            mode="existing",
            treasury_id=treasury_id.hex(),
            filter_amount=1,
        )
    )
    assert isinstance(dao_wallet_1, dict)
    assert dao_wallet_1.get("success")
    dao_wallet_1_id = dao_wallet_1["wallet_id"]
    # Create a cat wallet and add funds to treasury
    new_cat_amt = 1000000000000
    cat_wallet_0 = await api_0.create_new_wallet(
        dict(
            wallet_type="cat_wallet",
            name="CAT WALLET 1",
            mode="new",
            amount=new_cat_amt,
        )
    )
    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    cat_wallet_0_id = cat_wallet_0["wallet_id"]
    cat_id = bytes32.from_hexstr(cat_wallet_0["asset_id"])

    await rpc_state(
        20,
        api_0.get_wallet_balance,
        [{"wallet_id": cat_wallet_0_id}],
        lambda x: x["wallet_balance"]["confirmed_wallet_balance"],
        new_cat_amt,
    )

    cat_funding_amt = 500000
    await api_0.dao_add_funds_to_treasury(
        dict(
            wallet_id=dao_wallet_0_id,
            amount=cat_funding_amt,
            funding_wallet_id=cat_wallet_0_id,
        )
    )

    xch_funding_amt = 200000
    await api_0.dao_add_funds_to_treasury(
        dict(
            wallet_id=dao_wallet_0_id,
            amount=xch_funding_amt,
            funding_wallet_id=1,
        )
    )
    tx_queue = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    expected_xch -= xch_funding_amt + new_cat_amt
    await time_out_assert(30, wallet_0.get_confirmed_balance, expected_xch)

    await rpc_state(
        20,
        api_0.get_wallet_balance,
        [{"wallet_id": cat_wallet_0_id}],
        lambda x: x["wallet_balance"]["confirmed_wallet_balance"],
        new_cat_amt - cat_funding_amt,
    )

    balances = await api_1.dao_get_treasury_balance({"wallet_id": dao_wallet_1_id})
    assert balances["balances"]["xch"] == xch_funding_amt
    assert balances["balances"][cat_id.hex()] == cat_funding_amt

    # Send some cats to wallet_1
    await api_0.cat_spend(
        {
            "wallet_id": dao_cat_wallet_0_id,
            "amount": cat_amt // 2,
            "inner_address": encode_puzzle_hash(ph_1, "xch"),
            "fee": fee,
        }
    )
    tx_queue = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    await rpc_state(
        20,
        api_0.get_wallet_balance,
        [{"wallet_id": dao_cat_wallet_0_id}],
        lambda x: x["wallet_balance"]["confirmed_wallet_balance"],
        cat_amt // 2,
    )

    # send cats to lockup
    await api_0.dao_send_to_lockup({"wallet_id": dao_wallet_0_id, "amount": cat_amt // 2})
    tx_queue = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    await api_1.dao_send_to_lockup({"wallet_id": dao_wallet_1_id, "amount": cat_amt // 2})
    tx_queue = await wallet_node_1.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # create the first proposal
    additions = [
        {"puzzle_hash": ph_1.hex(), "amount": 1000},
    ]
    create_proposal = await api_0.dao_create_proposal(
        {
            "wallet_id": dao_wallet_0_id,
            "proposal_type": "spend",
            "additions": additions,
            "vote_amount": cat_amt // 2,
            "fee": fee,
        }
    )
    assert create_proposal["success"]
    tx_queue = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await rpc_state(20, api_0.dao_get_proposals, [{"wallet_id": dao_wallet_0_id}], lambda x: len(x["proposals"]), 1)

    await rpc_state(20, api_1.dao_get_proposals, [{"wallet_id": dao_wallet_1_id}], lambda x: len(x["proposals"]), 1)

    props_0 = await api_0.dao_get_proposals({"wallet_id": dao_wallet_0_id})
    prop = props_0["proposals"][0]
    assert prop.amount_voted == cat_amt // 2
    assert prop.yes_votes == cat_amt // 2

    state = await api_0.dao_get_proposal_state({"wallet_id": dao_wallet_0_id, "proposal_id": prop.proposal_id.hex()})
    assert state["state"]["passed"]
    assert not state["state"]["closable"]

    # Add votes
    await api_1.dao_vote_on_proposal(
        {
            "wallet_id": dao_wallet_1_id,
            "vote_amount": cat_amt // 2,
            "proposal_id": prop.proposal_id.hex(),
            "is_yes_vote": True,
        }
    )
    tx_queue = await wallet_node_1.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await rpc_state(
        20, api_0.dao_get_proposals, [{"wallet_id": dao_wallet_0_id}], lambda x: x["proposals"][0].amount_voted, cat_amt
    )

    # farm blocks until we can close proposal
    for _ in range(1, state["state"]["blocks_needed"]):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await rpc_state(
        20,
        api_0.dao_get_proposal_state,
        [{"wallet_id": dao_wallet_0_id, "proposal_id": prop.proposal_id.hex()}],
        lambda x: x["state"]["closable"],
        True,
    )

    await api_0.dao_close_proposal({"wallet_id": dao_wallet_0_id, "proposal_id": prop.proposal_id.hex()})
    tx_queue = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    await full_node_api.process_transaction_records(records=[tx for tx in tx_queue])
    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await rpc_state(
        20, api_0.dao_get_proposals, [{"wallet_id": dao_wallet_0_id}], lambda x: x["proposals"][0].closed, True
    )

    # check that the proposal state has changed for everyone
    await rpc_state(
        20,
        api_0.dao_get_proposal_state,
        [{"wallet_id": dao_wallet_0_id, "proposal_id": prop.proposal_id.hex()}],
        lambda x: x["state"]["closed"],
        True,
    )

    await rpc_state(
        20,
        api_1.dao_get_proposal_state,
        [{"wallet_id": dao_wallet_1_id, "proposal_id": prop.proposal_id.hex()}],
        lambda x: x["state"]["closed"],
        True,
    )


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_rpc_client(
    two_wallet_nodes_services: SimulatorsAndWalletsServices, trusted: bool, self_hostname: str
) -> None:
    num_blocks = 3
    [full_node_service], wallet_services, bt = two_wallet_nodes_services
    full_node_api = full_node_service._api
    full_node_server = full_node_api.full_node.server
    wallet_node_0 = wallet_services[0]._node
    server_0 = wallet_node_0.server
    wallet_node_1 = wallet_services[1]._node
    server_1 = wallet_node_1.server
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    ph_0 = await wallet_0.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_1.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_0))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    initial_funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )

    await time_out_assert(15, wallet_0.get_confirmed_balance, initial_funds)
    await time_out_assert(15, wallet_0.get_unconfirmed_balance, initial_funds)

    assert wallet_services[0].rpc_server is not None
    assert wallet_services[1].rpc_server is not None

    client_0 = await WalletRpcClient.create(
        self_hostname,
        wallet_services[0].rpc_server.listen_port,
        wallet_services[0].root_path,
        wallet_services[0].config,
    )
    await validate_get_routes(client_0, wallet_services[0].rpc_server.rpc_api)
    client_1 = await WalletRpcClient.create(
        self_hostname,
        wallet_services[1].rpc_server.listen_port,
        wallet_services[1].root_path,
        wallet_services[1].config,
    )
    await validate_get_routes(client_1, wallet_services[1].rpc_server.rpc_api)

    try:
        cat_amt = uint64(150000)
        amount_of_cats = uint64(cat_amt * 2)
        dao_rules = DAORules(
            proposal_timelock=uint64(8),
            soft_close_length=uint64(4),
            attendance_required=uint64(1000),  # 10%
            pass_percentage=uint64(5100),  # 51%
            self_destruct_length=uint64(20),
            oracle_spend_delay=uint64(10),
            proposal_minimum_amount=uint64(1),
        )
        filter_amount = uint64(1)
        fee = uint64(10000)

        # create new dao
        dao_wallet_dict_0 = await client_0.create_new_dao_wallet(
            mode="new",
            dao_rules=dao_rules.to_json_dict(),
            amount_of_cats=amount_of_cats,
            filter_amount=filter_amount,
            name="DAO WALLET 0",
        )
        assert dao_wallet_dict_0["success"]
        dao_id_0 = dao_wallet_dict_0["wallet_id"]
        treasury_id_hex = dao_wallet_dict_0["treasury_id"]
        cat_wallet_0 = wallet_node_0.wallet_state_manager.wallets[dao_wallet_dict_0["cat_wallet_id"]]

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await time_out_assert(20, cat_wallet_0.get_confirmed_balance, amount_of_cats)

        # join dao
        dao_wallet_dict_1 = await client_1.create_new_dao_wallet(
            mode="existing", treasury_id=treasury_id_hex, filter_amount=filter_amount, name="DAO WALLET 1"
        )
        assert dao_wallet_dict_1["success"]
        dao_id_1 = dao_wallet_dict_1["wallet_id"]
        cat_wallet_1 = wallet_node_1.wallet_state_manager.wallets[dao_wallet_dict_1["cat_wallet_id"]]

        # fund treasury
        xch_funds = uint64(10000000000)
        funding_tx = await client_0.dao_add_funds_to_treasury(dao_id_0, 1, xch_funds)
        assert funding_tx["success"]
        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await rpc_state(20, client_0.dao_get_treasury_balance, [dao_id_0], lambda x: x["balances"]["xch"], xch_funds)

        # send cats to wallet 1
        await client_0.cat_spend(
            wallet_id=dao_wallet_dict_0["cat_wallet_id"],
            amount=cat_amt,
            inner_address=encode_puzzle_hash(ph_1, "xch"),
            fee=fee,
        )

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
            await asyncio.sleep(0.5)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await time_out_assert(20, cat_wallet_0.get_confirmed_balance, cat_amt)
        await time_out_assert(20, cat_wallet_1.get_confirmed_balance, cat_amt)

        # send cats to lockup
        lockup_0 = await client_0.dao_send_to_lockup(dao_id_0, cat_amt)
        lockup_1 = await client_1.dao_send_to_lockup(dao_id_1, cat_amt)
        assert lockup_0["success"]
        assert lockup_1["success"]

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
            await asyncio.sleep(0.5)

        # create a spend proposal
        additions = [
            {"puzzle_hash": ph_1.hex(), "amount": 1000},
        ]
        proposal = await client_0.dao_create_proposal(
            wallet_id=dao_id_0,
            proposal_type="spend",
            additions=additions,
            vote_amount=cat_amt,
            fee=fee,
        )
        assert proposal["success"]
        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        # check proposal is found by wallet 1
        await rpc_state(20, client_1.dao_get_proposals, [dao_id_1], lambda x: x["proposals"][0]["yes_votes"], cat_amt)
        props = await client_1.dao_get_proposals(dao_id_1)
        proposal_id_hex = props["proposals"][0]["proposal_id"]

        # vote spend
        vote = await client_1.dao_vote_on_proposal(
            wallet_id=dao_id_1, proposal_id=proposal_id_hex, vote_amount=cat_amt, is_yes_vote=True, fee=fee
        )
        assert vote["success"]
        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        # check updated proposal is found by wallet 0
        await rpc_state(
            20, client_0.dao_get_proposals, [dao_id_0], lambda x: x["proposals"][0]["yes_votes"], cat_amt * 2
        )

        # check proposal state and farm enough blocks to pass
        state = await client_0.dao_get_proposal_state(wallet_id=dao_id_0, proposal_id=proposal_id_hex)
        assert state["success"]
        assert state["state"]["passed"]

        for _ in range(0, state["state"]["blocks_needed"]):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        state = await client_0.dao_get_proposal_state(wallet_id=dao_id_0, proposal_id=proposal_id_hex)
        assert state["success"]
        assert state["state"]["closable"]

        # close the proposal
        close = await client_0.dao_close_proposal(
            wallet_id=dao_id_0, proposal_id=proposal_id_hex, self_destruct=False, fee=fee
        )
        assert close["success"]

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        # check proposal is closed
        await rpc_state(20, client_1.dao_get_proposals, [dao_id_1], lambda x: x["proposals"][0]["closed"], True)
        await rpc_state(20, client_0.dao_get_proposals, [dao_id_0], lambda x: x["proposals"][0]["closed"], True)

        # free locked cats from finished proposal
        res = await client_0.dao_free_coins_from_finished_proposals(wallet_id=dao_id_0)
        assert res["success"]
        sb_name = bytes32.from_hexstr(res["spend_name"])
        await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, sb_name)

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        bal = await client_0.get_wallet_balance(dao_wallet_dict_0["dao_cat_wallet_id"])
        assert bal["confirmed_wallet_balance"] == cat_amt

        exit = await client_0.dao_exit_lockup(dao_id_0)
        assert exit["success"]

    finally:
        client_0.close()
        client_1.close()
        await client_0.await_closed()
        await client_1.await_closed()


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_concurrency(self_hostname: str, three_wallet_nodes: SimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 2
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_node_2, server_2 = wallets[2]
    wallet = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_2.wallet_state_manager.main_wallet
    ph = await wallet.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()
    ph_2 = await wallet_2.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_2.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_2.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_2))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    cat_amt = 300000
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),  # 10%
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=uint64(101),
    )

    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet,
            uint64(cat_amt),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    # Get the full node sim to process the wallet creation spend
    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    # Farm enough blocks to pass the oracle_spend_delay and then complete the treasury eve spend
    for i in range(1, 11):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # get the cat wallets
    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    await time_out_assert(10, cat_wallet_0.get_confirmed_balance, cat_amt)

    # get the dao_cat wallet
    dao_cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.dao_cat_wallet_id]

    treasury_id = dao_wallet_0.dao_info.treasury_id

    # Create the other user's wallet from the treasury id
    dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_1.wallet_state_manager,
        wallet_1,
        treasury_id,
    )
    assert dao_wallet_1 is not None
    assert dao_wallet_1.dao_info.treasury_id == treasury_id

    # Create funding spends for xch
    xch_funds = uint64(500000)
    funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(xch_funds)
    assert isinstance(funding_tx, TransactionRecord)
    funding_sb = funding_tx.spend_bundle
    assert isinstance(funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, funding_sb.name())
    await full_node_api.process_transaction_records(records=[funding_tx])

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Check that the funding spend is recognized by both dao wallets
    await time_out_assert(10, dao_wallet_0.get_balance_by_asset_type, xch_funds)

    # Send some dao_cats to wallet_1
    # Get the cat wallets for wallet_1
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    dao_cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.dao_cat_wallet_id]
    assert cat_wallet_1
    assert dao_cat_wallet_1

    # Add a third wallet and check they can find proposal with accurate vote counts
    dao_wallet_2 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_2.wallet_state_manager,
        wallet_2,
        treasury_id,
    )
    assert dao_wallet_2 is not None
    assert dao_wallet_2.dao_info.treasury_id == treasury_id

    dao_cat_wallet_2 = dao_wallet_2.wallet_state_manager.wallets[dao_wallet_2.dao_info.dao_cat_wallet_id]
    assert dao_cat_wallet_2

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    cat_tx = await cat_wallet_0.generate_signed_transaction([100000, 100000], [ph_1, ph_2])
    cat_sb = cat_tx[0].spend_bundle
    await wallet.wallet_state_manager.add_pending_transaction(cat_tx[0])
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_sb.name())
    await full_node_api.process_transaction_records(records=cat_tx)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    await time_out_assert(10, cat_wallet_1.get_confirmed_balance, 100000)
    cat_wallet_2 = dao_wallet_2.wallet_state_manager.wallets[dao_wallet_2.dao_info.cat_wallet_id]
    await time_out_assert(10, cat_wallet_2.get_confirmed_balance, 100000)
    await time_out_assert(10, cat_wallet_0.get_confirmed_balance, 100000)

    # Create dao cats for voting
    dao_cat_0_bal = await dao_cat_wallet_0.get_votable_balance()
    assert dao_cat_0_bal == 100000
    txs, new_dao_cats = await dao_cat_wallet_0.create_new_dao_cats(dao_cat_0_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Create a proposal for xch spend
    recipient_puzzle_hash = await wallet_2.get_new_puzzlehash()
    proposal_amount = uint64(10000)
    xch_proposal_inner = dao_wallet_0.generate_simple_proposal_innerpuz(
        [recipient_puzzle_hash],
        [proposal_amount],
        [None],
    )
    proposal_tx = await dao_wallet_0.generate_new_proposal(xch_proposal_inner, dao_cat_0_bal, uint64(1000))
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Check the proposal is saved
    assert len(dao_wallet_0.dao_info.proposals_list) == 1
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal
    assert dao_wallet_0.dao_info.proposals_list[0].timer_coin is not None

    # Check that wallet_1 also finds and saved the proposal
    assert len(dao_wallet_1.dao_info.proposals_list) == 1
    prop = dao_wallet_1.dao_info.proposals_list[0]

    # Give the wallet nodes a second
    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    total_votes = dao_cat_0_bal

    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_0.dao_info.proposals_list[0].yes_votes == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].yes_votes == total_votes

    # Create votable dao cats and add a new vote
    dao_cat_1_bal = await dao_cat_wallet_1.get_votable_balance()
    txs, new_dao_cats = await dao_cat_wallet_1.create_new_dao_cats(dao_cat_1_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)
    txs, new_dao_cats = await dao_cat_wallet_2.create_new_dao_cats(dao_cat_1_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    vote_sb = await dao_wallet_1.generate_proposal_vote_spend(prop.proposal_id, dao_cat_1_bal, True, push=True)
    vote_2 = await dao_wallet_2.generate_proposal_vote_spend(prop.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb.name())
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_2.name())

    await time_out_assert(20, len, 1, dao_wallet_2.dao_info.proposals_list)
    await time_out_assert(20, int, total_votes, dao_wallet_1.dao_info.proposals_list[0].amount_voted)
    await time_out_assert(20, int, total_votes, dao_wallet_2.dao_info.proposals_list[0].amount_voted)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await time_out_assert(20, int, total_votes * 2, dao_wallet_1.dao_info.proposals_list[0].amount_voted)
    await time_out_assert(20, int, total_votes * 2, dao_wallet_2.dao_info.proposals_list[0].amount_voted)
    dao_cat_1_bal = await dao_cat_wallet_1.get_votable_balance(prop.proposal_id)
    dao_cat_2_bal = await dao_cat_wallet_2.get_votable_balance(prop.proposal_id)

    assert (dao_cat_1_bal == 100000 and dao_cat_2_bal == 0) or (dao_cat_1_bal == 0 and dao_cat_2_bal == 100000)


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_cat_exits(
    two_wallet_nodes_services: SimulatorsAndWalletsServices, trusted: bool, self_hostname: str
) -> None:
    num_blocks = 3  # We're using the rpc client, so use 3 blocks to ensure we stay synced
    [full_node_service], wallet_services, bt = two_wallet_nodes_services
    full_node_api = full_node_service._api
    full_node_server = full_node_api.full_node.server
    wallet_node_0 = wallet_services[0]._node
    server_0 = wallet_node_0.server
    wallet_node_1 = wallet_services[1]._node
    server_1 = wallet_node_1.server
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    ph_0 = await wallet_0.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_1.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_0))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    initial_funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )

    await time_out_assert(15, wallet_0.get_confirmed_balance, initial_funds)
    await time_out_assert(15, wallet_0.get_unconfirmed_balance, initial_funds)

    assert wallet_services[0].rpc_server is not None
    assert wallet_services[1].rpc_server is not None

    client_0 = await WalletRpcClient.create(
        self_hostname,
        wallet_services[0].rpc_server.listen_port,
        wallet_services[0].root_path,
        wallet_services[0].config,
    )
    await validate_get_routes(client_0, wallet_services[0].rpc_server.rpc_api)
    client_1 = await WalletRpcClient.create(
        self_hostname,
        wallet_services[1].rpc_server.listen_port,
        wallet_services[1].root_path,
        wallet_services[1].config,
    )
    await validate_get_routes(client_1, wallet_services[1].rpc_server.rpc_api)

    try:
        cat_amt = uint64(150000)
        amount_of_cats = cat_amt
        dao_rules = DAORules(
            proposal_timelock=uint64(8),
            soft_close_length=uint64(4),
            attendance_required=uint64(1000),  # 10%
            pass_percentage=uint64(5100),  # 51%
            self_destruct_length=uint64(20),
            oracle_spend_delay=uint64(10),
            proposal_minimum_amount=uint64(1),
        )
        filter_amount = uint64(1)
        fee = uint64(10000)

        # create new dao
        dao_wallet_dict_0 = await client_0.create_new_dao_wallet(
            mode="new",
            dao_rules=dao_rules.to_json_dict(),
            amount_of_cats=amount_of_cats,
            filter_amount=filter_amount,
            name="DAO WALLET 0",
        )
        assert dao_wallet_dict_0["success"]
        dao_id_0 = dao_wallet_dict_0["wallet_id"]
        # treasury_id_hex = dao_wallet_dict_0["treasury_id"]
        cat_wallet_0 = wallet_node_0.wallet_state_manager.wallets[dao_wallet_dict_0["cat_wallet_id"]]
        dao_cat_wallet_0 = wallet_node_0.wallet_state_manager.wallets[dao_wallet_dict_0["dao_cat_wallet_id"]]

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await time_out_assert(20, cat_wallet_0.get_confirmed_balance, amount_of_cats)

        # fund treasury
        xch_funds = uint64(10000000000)
        funding_tx = await client_0.dao_add_funds_to_treasury(dao_id_0, 1, xch_funds)
        assert funding_tx["success"]
        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await rpc_state(20, client_0.dao_get_treasury_balance, [dao_id_0], lambda x: x["balances"]["xch"], xch_funds)

        # send cats to lockup
        lockup_0 = await client_0.dao_send_to_lockup(dao_id_0, cat_amt)
        assert lockup_0["success"]

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
            await asyncio.sleep(0.5)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        assert isinstance(dao_cat_wallet_0, DAOCATWallet)
        await time_out_assert(20, dao_cat_wallet_0.get_votable_balance, cat_amt)

        # create a spend proposal
        additions = [
            {"puzzle_hash": ph_1.hex(), "amount": 1000},
        ]
        proposal = await client_0.dao_create_proposal(
            wallet_id=dao_id_0,
            proposal_type="spend",
            additions=additions,
            vote_amount=cat_amt,
            fee=fee,
        )
        assert proposal["success"]
        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
            await asyncio.sleep(0.5)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await time_out_assert_not_none(20, client_0.dao_get_proposals, dao_id_0)
        props = await client_0.dao_get_proposals(dao_id_0)
        proposal_id_hex = props["proposals"][0]["proposal_id"]

        # check proposal state and farm enough blocks to pass
        state = await client_0.dao_get_proposal_state(wallet_id=dao_id_0, proposal_id=proposal_id_hex)
        assert state["success"]
        assert state["state"]["passed"]

        for _ in range(0, state["state"]["blocks_needed"]):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        state = await client_0.dao_get_proposal_state(wallet_id=dao_id_0, proposal_id=proposal_id_hex)
        assert state["success"]
        assert state["state"]["closable"]

        # close the proposal
        close = await client_0.dao_close_proposal(
            wallet_id=dao_id_0, proposal_id=proposal_id_hex, self_destruct=False, fee=fee
        )
        assert close["success"]

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        # check proposal is closed
        await rpc_state(20, client_0.dao_get_proposals, [dao_id_0], lambda x: x["proposals"][0]["closed"], True)

        # free locked cats from finished proposal
        res = await client_0.dao_free_coins_from_finished_proposals(wallet_id=dao_id_0)
        assert res["success"]
        sb_name = bytes32.from_hexstr(res["spend_name"])
        await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, sb_name)

        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        assert isinstance(dao_cat_wallet_0, DAOCATWallet)
        assert dao_cat_wallet_0.dao_cat_info.locked_coins[0].active_votes == []

        exit = await client_0.dao_exit_lockup(dao_id_0)
        assert exit["success"]
        for i in range(1, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
        await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

        await time_out_assert(20, dao_cat_wallet_0.get_confirmed_balance, 0)
        await time_out_assert(20, cat_wallet_0.get_confirmed_balance, cat_amt)

    finally:
        client_0.close()
        client_1.close()
        await client_0.await_closed()
        await client_1.await_closed()


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_reorgs(self_hostname: str, two_wallet_nodes: SimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 2
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    ph = await wallet.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    cat_amt = 300000
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),  # 10%
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=uint64(101),
    )

    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet,
            uint64(cat_amt),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    # Get the full node sim to process the wallet creation spend
    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    # Farm enough blocks to pass the oracle_spend_delay and then complete the treasury eve spend
    for i in range(10):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    assert (await dao_wallet_0.get_confirmed_balance()) == uint128(1)
    assert (await dao_wallet_0.get_unconfirmed_balance()) == uint128(1)
    assert (await dao_wallet_0.get_pending_change_balance()) == uint64(0)
    assert (await dao_wallet_0.get_spendable_balance()) == uint128(1)

    # Test Reorg on creation
    height = full_node_api.full_node.blockchain.get_peak_height()
    if height is None:
        assert False
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 10), uint32(height + 1), puzzle_hash_0, None)
    )
    assert (await dao_wallet_0.get_confirmed_balance()) == uint128(1)
    assert (await dao_wallet_0.get_unconfirmed_balance()) == uint128(1)
    assert (await dao_wallet_0.get_pending_change_balance()) == uint64(0)
    assert (await dao_wallet_0.get_spendable_balance()) == uint128(1)

    # get the cat wallets
    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    await time_out_assert(10, cat_wallet_0.get_confirmed_balance, cat_amt)

    # get the dao_cat wallet
    dao_cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.dao_cat_wallet_id]

    treasury_id = dao_wallet_0.dao_info.treasury_id

    # Create the other user's wallet from the treasury id
    dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_1.wallet_state_manager,
        wallet_1,
        treasury_id,
    )
    assert dao_wallet_1 is not None
    assert dao_wallet_1.dao_info.treasury_id == treasury_id

    # Create funding spends for xch
    xch_funds = uint64(500000)
    funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(xch_funds)
    assert isinstance(funding_tx, TransactionRecord)
    funding_sb = funding_tx.spend_bundle
    assert isinstance(funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, funding_sb.name())
    await full_node_api.process_transaction_records(records=[funding_tx])

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Check that the funding spend is recognized by both dao wallets
    await time_out_assert(10, dao_wallet_0.get_balance_by_asset_type, xch_funds)
    await time_out_assert(10, dao_wallet_1.get_balance_by_asset_type, xch_funds)

    # Reorg funding spend
    height = full_node_api.full_node.blockchain.get_peak_height()
    if height is None:
        assert False
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 1), uint32(height + 1), puzzle_hash_0, None)
    )
    await time_out_assert(10, dao_wallet_0.get_balance_by_asset_type, xch_funds)
    await time_out_assert(10, dao_wallet_1.get_balance_by_asset_type, xch_funds)

    # Send some dao_cats to wallet_1
    # Get the cat wallets for wallet_1
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    dao_cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.dao_cat_wallet_id]
    assert cat_wallet_1
    assert dao_cat_wallet_1

    cat_tx = await cat_wallet_0.generate_signed_transaction([100000], [ph_1])
    cat_sb = cat_tx[0].spend_bundle
    await wallet.wallet_state_manager.add_pending_transaction(cat_tx[0])
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_sb.name())
    await full_node_api.process_transaction_records(records=cat_tx)

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    await time_out_assert(10, cat_wallet_1.get_confirmed_balance, 100000)

    # Create dao cats for voting
    dao_cat_0_bal = await dao_cat_wallet_0.get_votable_balance()
    assert dao_cat_0_bal == 200000
    txs, new_dao_cats = await dao_cat_wallet_0.create_new_dao_cats(dao_cat_0_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Create a proposal for xch spend
    recipient_puzzle_hash = await wallet.get_new_puzzlehash()
    proposal_amount = uint64(10000)
    xch_proposal_inner = dao_wallet_0.generate_simple_proposal_innerpuz(
        [recipient_puzzle_hash],
        [proposal_amount],
        [None],
    )
    proposal_tx = await dao_wallet_0.generate_new_proposal(xch_proposal_inner, dao_cat_0_bal, uint64(1000))
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    # Check the proposal is saved
    assert len(dao_wallet_0.dao_info.proposals_list) == 1
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal
    assert dao_wallet_0.dao_info.proposals_list[0].timer_coin is not None

    # Reorg proposal creation
    height = full_node_api.full_node.blockchain.get_peak_height()
    if height is None:
        assert False
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 1), uint32(height + 1), puzzle_hash_0, None)
    )
    assert len(dao_wallet_0.dao_info.proposals_list) == 1
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal
    assert dao_wallet_0.dao_info.proposals_list[0].timer_coin is not None

    # Check that wallet_1 also finds and saved the proposal
    assert len(dao_wallet_1.dao_info.proposals_list) == 1
    prop = dao_wallet_1.dao_info.proposals_list[0]

    total_votes = dao_cat_0_bal

    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_0.dao_info.proposals_list[0].yes_votes == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].amount_voted == total_votes
    assert dao_wallet_1.dao_info.proposals_list[0].yes_votes == total_votes

    # Create votable dao cats and add a new vote
    dao_cat_1_bal = await dao_cat_wallet_1.get_votable_balance()
    txs, new_dao_cats = await dao_cat_wallet_1.create_new_dao_cats(dao_cat_1_bal, True)
    dao_cat_sb = txs[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb.name())
    await full_node_api.process_transaction_records(records=txs)

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    vote_sb = await dao_wallet_1.generate_proposal_vote_spend(prop.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal + dao_cat_1_bal
    assert dao_wallet_0.dao_info.proposals_list[0].yes_votes == dao_cat_0_bal + dao_cat_1_bal
    assert dao_wallet_1.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal + dao_cat_1_bal
    assert dao_wallet_1.dao_info.proposals_list[0].yes_votes == dao_cat_0_bal + dao_cat_1_bal

    # Reorg on vote spend
    height = full_node_api.full_node.blockchain.get_peak_height()
    if height is None:
        assert False
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 1), uint32(height + 1), puzzle_hash_0, None)
    )
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal + dao_cat_1_bal
    assert dao_wallet_0.dao_info.proposals_list[0].yes_votes == dao_cat_0_bal + dao_cat_1_bal
    assert dao_wallet_1.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal + dao_cat_1_bal
    assert dao_wallet_1.dao_info.proposals_list[0].yes_votes == dao_cat_0_bal + dao_cat_1_bal

    # Close proposal
    for i in range(5):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    close_sb = await dao_wallet_0.create_proposal_close_spend(prop.proposal_id, fee=uint64(100), push=True)
    await time_out_assert_not_none(20, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_0, 0])
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_1, 0])

    # Reorg closed proposal
    height = full_node_api.full_node.blockchain.get_peak_height()
    if height is None:
        assert False
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 1), uint32(height + 1), puzzle_hash_0, None)
    )
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_0, 0])
    await time_out_assert(20, get_proposal_state, (True, True), *[dao_wallet_1, 0])


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_dao_minting_proposal(
    self_hostname: str, three_wallet_nodes: SimulatorsAndWallets, trusted: bool
) -> None:
    num_blocks = 1
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_node_2, server_2 = wallets[2]
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_2.wallet_state_manager.main_wallet
    ph_0 = await wallet_0.get_new_puzzlehash()
    ph_1 = await wallet_1.get_new_puzzlehash()
    ph_2 = await wallet_2.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_2.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_2.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_0))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_2))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)]
    )

    await time_out_assert(20, wallet_0.get_confirmed_balance, funds)
    await time_out_assert(20, full_node_api.wallet_is_synced, True, wallet_node_0)

    # set a standard fee amount to use in all txns
    base_fee = uint64(100)

    # set the cat issuance and DAO rules
    cat_issuance = 300000
    proposal_min_amt = uint64(101)
    dao_rules = DAORules(
        proposal_timelock=uint64(10),
        soft_close_length=uint64(5),
        attendance_required=uint64(1000),
        pass_percentage=uint64(5100),  # 51%
        self_destruct_length=uint64(20),
        oracle_spend_delay=uint64(10),
        proposal_minimum_amount=proposal_min_amt,
    )

    # Create the DAO.
    # This takes two steps: create the treasury singleton, wait for oracle_spend_delay and
    # then complete the eve spend
    async with wallet_node_0.wallet_state_manager.lock:
        dao_wallet_0 = await DAOWallet.create_new_dao_and_wallet(
            wallet_node_0.wallet_state_manager,
            wallet_0,
            uint64(cat_issuance),
            dao_rules,
        )
        assert dao_wallet_0 is not None

    tx_queue: List[TransactionRecord] = await wallet_node_0.wallet_state_manager.tx_store.get_not_sent()
    tx_record = tx_queue[0]
    await full_node_api.process_transaction_records(records=[tx_record])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))

    # Farm enough blocks to pass the oracle_spend_delay and then complete the treasury eve spend
    for i in range(1, 11):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.cat_wallet_id]
    dao_cat_wallet_0 = dao_wallet_0.wallet_state_manager.wallets[dao_wallet_0.dao_info.dao_cat_wallet_id]
    await time_out_assert(10, cat_wallet_0.get_confirmed_balance, cat_issuance)
    assert dao_cat_wallet_0

    treasury_id = dao_wallet_0.dao_info.treasury_id

    # Create dao_wallet_1 from the treasury id
    dao_wallet_1 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_1.wallet_state_manager,
        wallet_1,
        treasury_id,
    )
    assert dao_wallet_1 is not None
    assert dao_wallet_1.dao_info.treasury_id == treasury_id
    cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.cat_wallet_id]
    dao_cat_wallet_1 = dao_wallet_1.wallet_state_manager.wallets[dao_wallet_1.dao_info.dao_cat_wallet_id]
    assert cat_wallet_1
    assert dao_cat_wallet_1

    # Create dao_wallet_2 from the treasury id
    dao_wallet_2 = await DAOWallet.create_new_dao_wallet_for_existing_dao(
        wallet_node_2.wallet_state_manager,
        wallet_2,
        treasury_id,
    )
    assert dao_wallet_2 is not None
    assert dao_wallet_2.dao_info.treasury_id == treasury_id
    cat_wallet_2 = dao_wallet_2.wallet_state_manager.wallets[dao_wallet_2.dao_info.cat_wallet_id]
    dao_cat_wallet_2 = dao_wallet_2.wallet_state_manager.wallets[dao_wallet_2.dao_info.dao_cat_wallet_id]
    assert cat_wallet_2
    assert dao_cat_wallet_2

    # Send 100k cats to wallet_1 and wallet_2
    cat_amt = uint64(100000)
    cat_tx = await cat_wallet_0.generate_signed_transaction([cat_amt, cat_amt], [ph_1, ph_2], fee=base_fee)
    cat_sb = cat_tx[0].spend_bundle
    await wallet_0.wallet_state_manager.add_pending_transaction(cat_tx[0])
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, cat_sb.name())
    await full_node_api.process_transaction_records(records=cat_tx)
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Lockup voting cats for all wallets
    dao_cat_0_bal = await dao_cat_wallet_0.get_votable_balance()
    txs_0, _ = await dao_cat_wallet_0.create_new_dao_cats(dao_cat_0_bal, True, fee=base_fee)
    dao_cat_sb_0 = txs_0[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb_0.name())
    await full_node_api.process_transaction_records(records=txs_0)

    dao_cat_1_bal = await dao_cat_wallet_1.get_votable_balance()
    txs_1, _ = await dao_cat_wallet_1.create_new_dao_cats(dao_cat_1_bal, True)
    dao_cat_sb_1 = txs_1[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb_1.name())
    await full_node_api.process_transaction_records(records=txs_1)

    dao_cat_2_bal = await dao_cat_wallet_2.get_votable_balance()
    txs_2, _ = await dao_cat_wallet_2.create_new_dao_cats(dao_cat_2_bal, True)
    dao_cat_sb_2 = txs_2[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, dao_cat_sb_2.name())
    await full_node_api.process_transaction_records(records=txs_2)

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    await time_out_assert(10, dao_cat_wallet_0.get_confirmed_balance, cat_amt)
    await time_out_assert(10, dao_cat_wallet_1.get_confirmed_balance, cat_amt)
    await time_out_assert(10, dao_cat_wallet_2.get_confirmed_balance, cat_amt)

    # Create funding spend so the treasury holds some XCH
    xch_funds = uint64(500000)
    funding_tx = await dao_wallet_0.create_add_money_to_treasury_spend(xch_funds)
    funding_sb = funding_tx.spend_bundle
    assert isinstance(funding_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, funding_sb.name())
    await full_node_api.process_transaction_records(records=[funding_tx])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    # Check that the funding spend is recognized by all wallets
    await time_out_assert(10, dao_wallet_0.get_balance_by_asset_type, xch_funds)
    await time_out_assert(10, dao_wallet_1.get_balance_by_asset_type, xch_funds)
    await time_out_assert(10, dao_wallet_2.get_balance_by_asset_type, xch_funds)

    # Create Proposals

    # Proposal 0: Spend xch to wallet_2.
    recipient_puzzle_hash = await wallet_2.get_new_puzzlehash()
    proposal_amount_1 = uint64(9998)
    xch_proposal_inner = dao_wallet_0.generate_simple_proposal_innerpuz(
        [recipient_puzzle_hash],
        [proposal_amount_1],
        [None],
    )
    proposal_tx = await dao_wallet_0.generate_new_proposal(xch_proposal_inner, dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)

    assert len(dao_wallet_0.dao_info.proposals_list) == 1
    assert dao_wallet_0.dao_info.proposals_list[0].amount_voted == dao_cat_0_bal
    assert dao_wallet_0.dao_info.proposals_list[0].timer_coin is not None

    # Proposal 1: Mint new CATs
    recipient_puzzle_hash = await wallet_2.get_new_puzzlehash()
    new_mint_amount = uint64(500)
    mint_proposal_inner = await dao_wallet_0.generate_mint_proposal_innerpuz(
        new_mint_amount,
        recipient_puzzle_hash,
    )

    proposal_tx = await dao_wallet_0.generate_new_proposal(mint_proposal_inner, vote_amount=dao_cat_0_bal, fee=base_fee)
    assert isinstance(proposal_tx, TransactionRecord)
    proposal_sb = proposal_tx.spend_bundle
    assert isinstance(proposal_sb, SpendBundle)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, proposal_sb.name())
    await full_node_api.process_spend_bundles(bundles=[proposal_sb])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    assert len(dao_wallet_0.dao_info.proposals_list) == 2
    prop_1 = dao_wallet_0.dao_info.proposals_list[1]

    # Proposal 1 vote and close
    vote_sb_1 = await dao_wallet_1.generate_proposal_vote_spend(prop_1.proposal_id, dao_cat_1_bal, True, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, vote_sb_1.name())
    await full_node_api.process_spend_bundles(bundles=[vote_sb_1])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)

    for _ in range(10):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)

    close_sb_1 = await dao_wallet_0.create_proposal_close_spend(prop_1.proposal_id, fee=base_fee, push=True)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, close_sb_1.name())
    await full_node_api.process_spend_bundles(bundles=[close_sb_1])
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(puzzle_hash_0))
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=30)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_2, timeout=30)