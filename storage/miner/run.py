# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 philanthrope

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
import time
import wandb
import bittensor as bt
import traceback
from substrateinterface import SubstrateInterface
from .set_weights import set_weights, should_wait_to_set_weights
from .utils import update_storage_stats


def run(self):
    """
    Initiates and manages the main loop for the miner on the Bittensor network.

    This function performs the following primary tasks:
    1. Check for registration on the Bittensor network.
    2. Attaches the miner's forward, blacklist, and priority functions to its axon.
    3. Starts the miner's axon, making it active on the network.
    4. Regularly updates the metagraph with the latest network state.
    5. Optionally sets weights on the network, defining how much trust to assign to other nodes.
    6. Handles graceful shutdown on keyboard interrupts and logs unforeseen errors.

    The miner continues its operations until `should_exit` is set to True or an external interruption occurs.
    During each epoch of its operation, the miner waits for new blocks on the Bittensor network, updates its
    knowledge of the network (metagraph), and sets its weights. This process ensures the miner remains active
    and up-to-date with the network's latest state.

    Note:
        - The function leverages the global configurations set during the initialization of the miner.
        - The miner's axon serves as its interface to the Bittensor network, handling incoming and outgoing requests.

    Raises:
        KeyboardInterrupt: If the miner is stopped by a manual interruption.
        Exception: For unforeseen errors during the miner's operation, which are logged for diagnosis.
    """
    substrate = SubstrateInterface(
        ss58_format=bt.__ss58_format__,
        use_remote_preset=True,
        url=self.subtensor.chain_endpoint,
        type_registry=bt.__type_registry__,
    )

    netuid = self.config.netuid

    # --- Check for registration.
    if not self.subtensor.is_hotkey_registered(
        netuid=netuid,
        hotkey_ss58=self.wallet.hotkey.ss58_address,
    ):
        bt.logging.error(
            f"Wallet: {self.wallet} is not registered on netuid {netuid}"
            f"Please register the hotkey using `btcli subnets register` before trying again"
        )
        exit()

    tempo = substrate.query(
        module="SubtensorModule", storage_function="Tempo", params=[netuid]
    ).value

    last_block_hash_submitted = None
    last_extrinsic_hash = None

    def handler(obj, update_nr, subscription_id):
        current_block = obj["header"]["number"]
        bt.logging.debug(f"New block #{current_block}")

        bt.logging.debug(
            f"Blocks since epoch: {(current_block + netuid + 1) % (tempo + 1)}"
        )

        nonlocal last_block_hash_submitted
        nonlocal last_extrinsic_hash

        if last_extrinsic_hash != None and last_block_hash_submitted != None:
            receipt = substrate.retrieve_extrinsic_by_hash(last_block_hash_submitted, last_extrinsic_hash)
            last_block_hash_submitted = None
            last_extrinsic_hash = None
            bt.logging.debug(receipt)

        if (current_block + netuid + 1) % (tempo + 1) == 0:
            bt.logging.info(
                f"New epoch started, setting weights at block {current_block}"
            )

            call = substrate.compose_call(
                call_module="SubtensorModule",
                call_function="set_weights",
                call_params={
                    "dests": [self.my_subnet_uid],
                    "weights": [65535],
                    "netuid": netuid,
                    "version_key": 1,
                },
            )
            # Period dictates how long the extrinsic will stay as part of waiting pool
            extrinsic = substrate.create_signed_extrinsic(
                call=call, keypair=self.wallet.hotkey, era={"period": 100}
            )
            response = substrate.submit_extrinsic(
                extrinsic,
                wait_for_inclusion=False,
                wait_for_finalization=False,
            )

            last_block_hash_submitted = obj["header"]["hash"]
            last_extrinsic_hash = response.extrinsic_hash

            if response:
                bt.logging.info("Setting self-weights on chain successful")

            # --- Update the miner storage information periodically.
            update_storage_stats(self)
            bt.logging.debug("Storage statistics updated...")

            if self.should_exit:
                return True

    substrate.subscribe_block_headers(handler)
