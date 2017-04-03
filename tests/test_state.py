import pytest

import fnmatch
import json
import os

from trie import (
    Trie,
)
from trie.db.memory import (
    MemoryDB,
)

from eth_utils import (
    is_0x_prefixed,
    to_canonical_address,
    decode_hex,
    pad_left,
    keccak,
)

from evm.constants import (
    ZERO_ADDRESS,
)
from evm.preconfigured.genesis import (
    GENESIS_OPCODES,
)
from evm.rlp.header import (
    BlockHeader,
)
from evm.rlp.transaction import (
    Transaction,
    UnsignedTransaction,
)
from evm.vm import (
    Message,
    EVM,
)

from evm.utils.address import (
    private_key_to_address,
)
from evm.utils.numeric import (
    int_to_big_endian,
)
from evm.utils.padding import (
    pad32,
)


def to_int(value):
    if is_0x_prefixed(value):
        if len(value) == 2:
            return 0
        else:
            return int(value, 16)
    else:
        return int(value)


def normalize_statetest_fixture(fixture):
    normalized_fixture = {
        'env': {
            'currentCoinbase': decode_hex(fixture['env']['currentCoinbase']),
            'currentDifficulty': to_int(fixture['env']['currentDifficulty']),
            'currentNumber': to_int(fixture['env']['currentNumber']),
            'currentGasLimit': to_int(fixture['env']['currentGasLimit']),
            'currentTimestamp': to_int(fixture['env']['currentTimestamp']),
            'previousHash': decode_hex(fixture['env']['previousHash']),
        },
        'transaction': {
            'data': decode_hex(fixture['transaction']['data']),
            'gasLimit': to_int(fixture['transaction']['gasLimit']),
            'gasPrice': to_int(fixture['transaction']['gasPrice']),
            'nonce': to_int(fixture['transaction']['nonce']),
            'secretKey': decode_hex(fixture['transaction']['secretKey']),
            'to': (
                to_canonical_address(fixture['transaction']['to'])
                if fixture['transaction']['to']
                else ZERO_ADDRESS
            ),
            'value': to_int(fixture['transaction']['value']),
        },
        'pre': {
            to_canonical_address(address): {
                'balance': to_int(state['balance']),
                'code': decode_hex(state['code']),
                'nonce': to_int(state['nonce']),
                'storage': {
                    pad32(int_to_big_endian(to_int(slot))): decode_hex(value)
                    for slot, value in state['storage'].items()
                },
            } for address, state in fixture['pre'].items()
        },
        'postStateRoot': decode_hex(fixture['postStateRoot']),
    }

    if 'post' in fixture:
        normalized_fixture['post'] = {
            to_canonical_address(address): {
                'balance': to_int(state['balance']),
                'code': decode_hex(state['code']),
                'nonce': to_int(state['nonce']),
                'storage': {
                    pad32(int_to_big_endian(to_int(slot))): decode_hex(value)
                    for slot, value in state['storage'].items()
                },
            } for address, state in fixture['post'].items()
        }

    if 'out' in fixture:
        if fixture['out'].startswith('#'):
            normalized_fixture['out'] = int(fixture['out'][1:])
        else:
            normalized_fixture['out'] = decode_hex(fixture['out'])

    if 'logs' in fixture:
        normalized_fixture['logs'] = [
            {
                'address': to_canonical_address(log_entry['address']),
                'topics': [decode_hex(topic) for topic in log_entry['topics']],
                'data': decode_hex(log_entry['data']),
                # 'bloom': decode_hex(log_entry['bloom']),  #TODO
            } for log_entry in fixture['logs']
        ]

    return normalized_fixture


GenesisEVM = EVM.create(name='genesis', opcode_classes=GENESIS_OPCODES)


ROOT_PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))


def recursive_find_files(base_dir, pattern):
    for dirpath, _, filenames in os.walk(base_dir):
        for filename in filenames:
            if fnmatch.fnmatch(filename, pattern):
                yield os.path.join(dirpath, filename)


BASE_FIXTURE_PATH = os.path.join(ROOT_PROJECT_DIR, 'fixtures', 'StateTests')


FIXTURES_PATHS = tuple(recursive_find_files(BASE_FIXTURE_PATH, "*.json"))


RAW_FIXTURES = tuple(
    (
        os.path.basename(fixture_path),
        json.load(open(fixture_path)),
    ) for fixture_path in FIXTURES_PATHS
)


SUCCESS_FIXTURES = tuple(
    (
        "{0}:{1}".format(fixture_filename, key),
        normalize_statetest_fixture(fixtures[key]),
    )
    for fixture_filename, fixtures in RAW_FIXTURES
    for key in sorted(fixtures.keys())
    if 'post' in fixtures[key]
)


#FAILURE_FIXTURES = tuple(
#    (
#        "{0}:{1}".format(fixture_filename, key),
#        normalize_statetest_fixture(fixtures[key]),
#    )
#    for fixture_filename, fixtures in RAW_FIXTURES
#    for key in sorted(fixtures.keys())
#    if 'post' not in fixtures[key]
#)

class EVMForTesting(GenesisEVM):
    #
    # Storage Overrides
    #
    def get_block_hash(self, block_number):
        if block_number >= self.environment.block_number:
            return b''
        elif block_number < self.environment.block_number - 256:
            return b''
        else:
            return keccak("{0}".format(block_number))


def setup_storage(fixture, storage):
    for account, account_data in fixture['pre'].items():
        for slot, value in account_data['storage'].items():
            storage.set_storage(account, slot, value)

        nonce = account_data['nonce']
        code = account_data['code']
        balance = account_data['balance']

        storage.set_nonce(account, nonce)
        storage.set_code(account, code)
        storage.set_balance(account, balance)
    return storage


ORIGIN = b'\x00' * 31 + b'\x01'


@pytest.mark.parametrize(
    'fixture_name,fixture', SUCCESS_FIXTURES,
)
def test_vm_success_using_fixture(fixture_name, fixture):
    environment = BlockHeader(
        coinbase=fixture['env']['currentCoinbase'],
        difficulty=fixture['env']['currentDifficulty'],
        block_number=fixture['env']['currentNumber'],
        gas_limit=fixture['env']['currentGasLimit'],
        timestamp=fixture['env']['currentTimestamp'],
        previous_hash=fixture['env']['previousHash'],
    )
    evm = EVMForTesting(
        db=Trie(MemoryDB()),
        environment=environment,
    )

    setup_storage(fixture, evm.storage)

    transaction = UnsignedTransaction(
        nonce=fixture['transaction']['nonce'],
        gas_price=fixture['transaction']['gasPrice'],
        gas=fixture['transaction']['gasLimit'],
        to=fixture['transaction']['to'],
        value=fixture['transaction']['value'],
        data=fixture['transaction']['data'],
    )
    computation = evm.apply_transaction(transaction)

    assert computation.error is None

    expected_logs = [
        {
            'address': log_entry[0],
            'topics': log_entry[1],
            'data': log_entry[2],
        }
        for log_entry in computation.logs
    ]
    expected_logs == fixture['logs']

    expected_output = fixture['out']
    if isinstance(expected_output, int):
        assert len(computation.output) == expected_output
    else:
        assert computation.output == expected_output

    assert evm.db.root_hash == fixture['postStateRoot']

    for account_as_hex, account_data in fixture['post'].items():
        account = to_canonical_address(account_as_hex)

        for slot, unpadded_expected_storage_value in account_data['storage'].items():
            expected_storage_value = pad_left(
                unpadded_expected_storage_value,
                32,
                b'\x00',
            )
            actual_storage_value = pad_left(
                evm.storage.get_storage(account, slot),
                32,
                b'\x00',
            )

            assert actual_storage_value == expected_storage_value

        expected_nonce = account_data['nonce']
        expected_code = account_data['code']
        expected_balance = account_data['balance']

        actual_nonce = evm.storage.get_nonce(account)
        actual_code = evm.storage.get_code(account)
        actual_balance = evm.storage.get_balance(account)

        assert actual_nonce == expected_nonce
        assert actual_code == expected_code
        assert actual_balance == expected_balance


#@pytest.mark.parametrize(
#    'fixture_name,fixture', FAILURE_FIXTURES,
#)
#def test_vm_failure_using_fixture(fixture_name, fixture):
#    environment = Environment(
#        coinbase=fixture['env']['currentCoinbase'],
#        difficulty=fixture['env']['currentDifficulty'],
#        block_number=fixture['env']['currentNumber'],
#        gas_limit=fixture['env']['currentGasLimit'],
#        timestamp=fixture['env']['currentTimestamp'],
#    )
#    evm = EVMForTesting(
#        storage=MemoryStorage(),
#        environment=environment,
#    )
#
#    assert fixture.get('callcreates', []) == []
#
#    setup_storage(fixture, evm.storage)
#
#    message = Message(
#        origin=fixture['exec']['origin'],
#        to=fixture['exec']['address'],
#        sender=fixture['exec']['caller'],
#        value=fixture['exec']['value'],
#        data=fixture['exec']['data'],
#        gas=fixture['exec']['gas'],
#        gas_price=fixture['exec']['gasPrice'],
#    )
#
#    computation = evm.apply_computation(message)
#    assert computation.error
#    assert isinstance(computation.error, VMError)
