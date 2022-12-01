from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sqlite3
from pathlib import Path
from secrets import token_bytes
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Iterator, List, Optional, Tuple, Union

from chia.cmds.init_funcs import init
from chia.consensus.constants import ConsensusConstants
from chia.daemon.server import WebSocketServer, daemon_launch_lock_path
from chia.farmer.farmer import Farmer
from chia.farmer.farmer_api import FarmerAPI
from chia.full_node.full_node import FullNode
from chia.full_node.full_node_api import FullNodeAPI
from chia.harvester.harvester import Harvester
from chia.harvester.harvester_api import HarvesterAPI
from chia.introducer.introducer import Introducer
from chia.introducer.introducer_api import IntroducerAPI
from chia.protocols.shared_protocol import Capability, capabilities
from chia.server.start_farmer import create_farmer_service
from chia.server.start_full_node import create_full_node_service
from chia.server.start_harvester import create_harvester_service
from chia.server.start_introducer import create_introducer_service
from chia.server.start_service import Service
from chia.server.start_timelord import create_timelord_service
from chia.server.start_wallet import create_wallet_service
from chia.simulator.block_tools import BlockTools
from chia.simulator.full_node_simulator import FullNodeSimulator
from chia.simulator.keyring import TempKeyring
from chia.simulator.start_simulator import create_full_node_simulator_service
from chia.timelord.timelord import Timelord
from chia.timelord.timelord_api import TimelordAPI
from chia.timelord.timelord_launcher import kill_processes, spawn_process
from chia.types.peer_info import PeerInfo
from chia.util.bech32m import encode_puzzle_hash
from chia.util.config import lock_and_load_config, save_config
from chia.util.ints import uint16
from chia.util.keychain import bytes_to_mnemonic
from chia.util.lock import Lockfile
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet_node_api import WalletNodeAPI

log = logging.getLogger(__name__)


def get_capabilities(disable_capabilities_values: Optional[List[Capability]]) -> List[Tuple[uint16, str]]:
    if disable_capabilities_values is not None:
        try:
            if Capability.BASE in disable_capabilities_values:
                # BASE capability cannot be removed
                disable_capabilities_values.remove(Capability.BASE)

            updated_capabilities = []
            for capability in capabilities:
                if Capability(int(capability[0])) in disable_capabilities_values:
                    # "0" means capability is disabled
                    updated_capabilities.append((capability[0], "0"))
                else:
                    updated_capabilities.append(capability)
            return updated_capabilities
        except Exception:
            logging.getLogger(__name__).exception("Error disabling capabilities, defaulting to all capabilities")
    return capabilities.copy()


async def setup_daemon(btools: BlockTools) -> AsyncGenerator[WebSocketServer, None]:
    root_path = btools.root_path
    config = btools.config
    assert "daemon_port" in config
    crt_path = root_path / config["daemon_ssl"]["private_crt"]
    key_path = root_path / config["daemon_ssl"]["private_key"]
    ca_crt_path = root_path / config["private_ssl_ca"]["crt"]
    ca_key_path = root_path / config["private_ssl_ca"]["key"]
    with Lockfile.create(daemon_launch_lock_path(root_path)):
        shutdown_event = asyncio.Event()
        ws_server = WebSocketServer(root_path, ca_crt_path, ca_key_path, crt_path, key_path, shutdown_event)
        await ws_server.start()

        yield ws_server

        await ws_server.stop()


@contextlib.contextmanager
def dbp(db_path: Path, db_version: int) -> Iterator[None]:
    if db_path.exists():
        db_path.unlink()

        if db_version > 1:
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE database_version(version int)")
                connection.execute("INSERT INTO database_version VALUES (?)", (db_version,))
                connection.commit()

    yield

    if db_path.exists():
        db_path.unlink()


@contextlib.asynccontextmanager
async def ms(service: Service[Any, Any]) -> AsyncIterator[None]:
    await service.start()

    yield

    service.stop()
    await service.wait_closed()


async def setup_full_node(
    consensus_constants: ConsensusConstants,
    db_name: str,
    self_hostname: str,
    local_bt: BlockTools,
    introducer_port: Optional[int] = None,
    send_uncompact_interval: int = 0,
    sanitize_weight_proof_only: bool = False,
    connect_to_daemon: bool = False,
    db_version: int = 1,
    disable_capabilities: Optional[List[Capability]] = None,
) -> AsyncGenerator[Service[FullNode, FullNodeAPI], None]:
    with dbp(db_path=local_bt.root_path / f"{db_name}", db_version=db_version):
        config, override_capabilities, updated_constants = await prepare_node(
            connect_to_daemon=connect_to_daemon,
            consensus_constants=consensus_constants,
            db_name=db_name,
            disable_capabilities=disable_capabilities,
            introducer_port=introducer_port,
            local_bt=local_bt,
            sanitize_weight_proof_only=sanitize_weight_proof_only,
            self_hostname=self_hostname,
            send_uncompact_interval=send_uncompact_interval,
        )
        service = create_full_node_service(
            local_bt.root_path,
            config,
            updated_constants,
            connect_to_daemon=connect_to_daemon,
            override_capabilities=override_capabilities,
        )

        async with ms(service=service):
            yield service


async def setup_full_node_simulator(
    consensus_constants: ConsensusConstants,
    db_name: str,
    self_hostname: str,
    local_bt: BlockTools,
    introducer_port: Optional[int] = None,
    send_uncompact_interval: int = 0,
    sanitize_weight_proof_only: bool = False,
    connect_to_daemon: bool = False,
    db_version: int = 1,
    disable_capabilities: Optional[List[Capability]] = None,
) -> AsyncGenerator[Service[FullNode, FullNodeSimulator], None]:
    with dbp(db_path=local_bt.root_path / f"{db_name}", db_version=db_version):
        config, override_capabilities, updated_constants = await prepare_node(
            connect_to_daemon=connect_to_daemon,
            consensus_constants=consensus_constants,
            db_name=db_name,
            disable_capabilities=disable_capabilities,
            introducer_port=introducer_port,
            local_bt=local_bt,
            sanitize_weight_proof_only=sanitize_weight_proof_only,
            self_hostname=self_hostname,
            send_uncompact_interval=send_uncompact_interval,
        )
        service = create_full_node_simulator_service(
            local_bt.root_path,
            config,
            local_bt,
            connect_to_daemon=connect_to_daemon,
            override_capabilities=override_capabilities,
        )

        async with ms(service=service):
            yield service


async def prepare_node(
    connect_to_daemon: bool,
    consensus_constants: ConsensusConstants,
    db_name: str,
    disable_capabilities: Optional[List[Capability]],
    introducer_port: Optional[int],
    local_bt: BlockTools,
    sanitize_weight_proof_only: bool,
    self_hostname: str,
    send_uncompact_interval: int,
) -> Tuple[Dict[str, Any], Union[List[Tuple[uint16, str]], None], ConsensusConstants]:
    if connect_to_daemon:
        assert local_bt.config["daemon_port"] is not None
    config = local_bt.config
    service_config = config["full_node"]
    service_config["database_path"] = db_name
    service_config["send_uncompact_interval"] = send_uncompact_interval
    service_config["target_uncompact_proofs"] = 30
    service_config["peer_connect_interval"] = 50
    service_config["sanitize_weight_proof_only"] = sanitize_weight_proof_only
    if introducer_port is not None:
        service_config["introducer_peer"]["host"] = self_hostname
        service_config["introducer_peer"]["port"] = introducer_port
    else:
        service_config["introducer_peer"] = None
    service_config["dns_servers"] = []
    service_config["port"] = 0
    service_config["rpc_port"] = 0
    config["simulator"]["auto_farm"] = False  # Disable Auto Farm for tests
    config["simulator"]["use_current_time"] = False  # Disable Real timestamps when running tests
    overrides = service_config["network_overrides"]["constants"][service_config["selected_network"]]
    updated_constants = consensus_constants.replace_str_to_bytes(**overrides)
    local_bt.change_config(config)
    override_capabilities = None if disable_capabilities is None else get_capabilities(disable_capabilities)
    return config, override_capabilities, updated_constants


# Note: convert these setup functions to fixtures, or push it one layer up,
# keeping these usable independently?
async def setup_wallet_node(
    self_hostname: str,
    consensus_constants: ConsensusConstants,
    local_bt: BlockTools,
    spam_filter_after_n_txs: Optional[int] = 200,
    xch_spam_amount: int = 1000000,
    full_node_port: Optional[uint16] = None,
    introducer_port: Optional[uint16] = None,
    key_seed: Optional[bytes] = None,
    initial_num_public_keys: int = 5,
) -> AsyncGenerator[Service[WalletNode, WalletNodeAPI], None]:
    with TempKeyring(populate=True) as keychain:
        config = local_bt.config
        service_config = config["wallet"]
        service_config["port"] = 0
        service_config["rpc_port"] = 0
        service_config["initial_num_public_keys"] = initial_num_public_keys
        service_config["spam_filter_after_n_txs"] = spam_filter_after_n_txs
        service_config["xch_spam_amount"] = xch_spam_amount

        entropy = token_bytes(32)
        if key_seed is None:
            key_seed = entropy
        keychain.add_private_key(bytes_to_mnemonic(key_seed))
        first_pk = keychain.get_first_public_key()
        assert first_pk is not None
        db_path_key_suffix = str(first_pk.get_fingerprint())
        db_name = f"test-wallet-db-{full_node_port}-KEY.sqlite"
        db_path_replaced: str = db_name.replace("KEY", db_path_key_suffix)
        db_path = local_bt.root_path / db_path_replaced

        if db_path.exists():
            db_path.unlink()
        service_config["database_path"] = str(db_name)
        service_config["testing"] = True

        service_config["introducer_peer"]["host"] = self_hostname
        if introducer_port is not None:
            service_config["introducer_peer"]["port"] = introducer_port
            service_config["peer_connect_interval"] = 10
        else:
            service_config["introducer_peer"] = None

        if full_node_port is not None:
            service_config["full_node_peer"] = {}
            service_config["full_node_peer"]["host"] = self_hostname
            service_config["full_node_peer"]["port"] = full_node_port
        else:
            del service_config["full_node_peer"]

        service = create_wallet_service(
            local_bt.root_path,
            config,
            consensus_constants,
            keychain,
            connect_to_daemon=False,
        )

        await service.start()

        yield service

        service.stop()
        await service.wait_closed()
        if db_path.exists():
            db_path.unlink()
        keychain.delete_all_keys()


async def setup_harvester(
    b_tools: BlockTools,
    root_path: Path,
    farmer_peer: Optional[PeerInfo],
    consensus_constants: ConsensusConstants,
    start_service: bool = True,
) -> AsyncGenerator[Service[Harvester, HarvesterAPI], None]:
    init(None, root_path)
    init(b_tools.root_path / "config" / "ssl" / "ca", root_path)
    with lock_and_load_config(root_path, "config.yaml") as config:
        config["logging"]["log_stdout"] = True
        config["selected_network"] = "testnet0"
        config["harvester"]["selected_network"] = "testnet0"
        config["harvester"]["port"] = 0
        config["harvester"]["rpc_port"] = 0
        config["harvester"]["plot_directories"] = [str(b_tools.plot_dir.resolve())]
        save_config(root_path, "config.yaml", config)
    service = create_harvester_service(
        root_path,
        config,
        consensus_constants,
        farmer_peer=farmer_peer,
        connect_to_daemon=False,
    )

    if start_service:
        await service.start()

    yield service

    service.stop()
    await service.wait_closed()


async def setup_farmer(
    b_tools: BlockTools,
    root_path: Path,
    self_hostname: str,
    consensus_constants: ConsensusConstants,
    full_node_port: Optional[uint16] = None,
    start_service: bool = True,
    port: uint16 = uint16(0),
) -> AsyncGenerator[Service[Farmer, FarmerAPI], None]:
    init(None, root_path)
    init(b_tools.root_path / "config" / "ssl" / "ca", root_path)
    with lock_and_load_config(root_path, "config.yaml") as root_config:
        root_config["logging"]["log_stdout"] = True
        root_config["selected_network"] = "testnet0"
        root_config["farmer"]["selected_network"] = "testnet0"
        save_config(root_path, "config.yaml", root_config)
    service_config = root_config["farmer"]
    config_pool = root_config["pool"]

    service_config["xch_target_address"] = encode_puzzle_hash(b_tools.farmer_ph, "xch")
    service_config["pool_public_keys"] = [bytes(pk).hex() for pk in b_tools.pool_pubkeys]
    service_config["port"] = port
    service_config["rpc_port"] = uint16(0)
    config_pool["xch_target_address"] = encode_puzzle_hash(b_tools.pool_ph, "xch")

    if full_node_port:
        service_config["full_node_peer"]["host"] = self_hostname
        service_config["full_node_peer"]["port"] = full_node_port
    else:
        del service_config["full_node_peer"]

    service = create_farmer_service(
        root_path,
        root_config,
        config_pool,
        consensus_constants,
        b_tools.local_keychain,
        connect_to_daemon=False,
    )

    if start_service:
        await service.start()

    yield service

    service.stop()
    await service.wait_closed()


async def setup_introducer(bt: BlockTools, port: int) -> AsyncGenerator[Service[Introducer, IntroducerAPI], None]:
    service = create_introducer_service(
        bt.root_path,
        bt.config,
        advertised_port=port,
        connect_to_daemon=False,
    )

    await service.start()

    yield service

    service.stop()
    await service.wait_closed()


async def setup_vdf_client(bt: BlockTools, self_hostname: str, port: int) -> AsyncGenerator[asyncio.Task[Any], None]:
    lock = asyncio.Lock()
    vdf_task_1 = asyncio.create_task(spawn_process(self_hostname, port, 1, lock, bt.config.get("prefer_ipv6")))

    def stop() -> None:
        asyncio.create_task(kill_processes(lock))

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop)
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop)

    yield vdf_task_1
    await kill_processes(lock)


async def setup_vdf_clients(
    bt: BlockTools, self_hostname: str, port: int
) -> AsyncGenerator[Tuple[asyncio.Task[Any], asyncio.Task[Any], asyncio.Task[Any]], None]:
    lock = asyncio.Lock()
    vdf_task_1 = asyncio.create_task(spawn_process(self_hostname, port, 1, lock, bt.config.get("prefer_ipv6")))
    vdf_task_2 = asyncio.create_task(spawn_process(self_hostname, port, 2, lock, bt.config.get("prefer_ipv6")))
    vdf_task_3 = asyncio.create_task(spawn_process(self_hostname, port, 3, lock, bt.config.get("prefer_ipv6")))

    def stop() -> None:
        asyncio.create_task(kill_processes(lock))

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop)
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop)

    yield vdf_task_1, vdf_task_2, vdf_task_3

    await kill_processes(lock)


async def setup_timelord(
    full_node_port: int,
    sanitizer: bool,
    consensus_constants: ConsensusConstants,
    b_tools: BlockTools,
    vdf_port: uint16 = uint16(0),
) -> AsyncGenerator[Service[Timelord, TimelordAPI], None]:
    config = b_tools.config
    service_config = config["timelord"]
    service_config["full_node_peer"]["port"] = full_node_port
    service_config["bluebox_mode"] = sanitizer
    service_config["fast_algorithm"] = False
    service_config["vdf_server"]["port"] = vdf_port
    service_config["start_rpc_server"] = True
    service_config["rpc_port"] = uint16(0)

    service = create_timelord_service(
        b_tools.root_path,
        config,
        consensus_constants,
        connect_to_daemon=False,
    )

    await service.start()

    yield service

    service.stop()
    await service.wait_closed()