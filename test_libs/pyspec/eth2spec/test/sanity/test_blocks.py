from copy import deepcopy

from eth2spec.utils.ssz.ssz_impl import signing_root
from eth2spec.utils.bls import bls_sign

from eth2spec.test.helpers.state import get_balance, state_transition_and_sign_block
from eth2spec.test.helpers.block import build_empty_block_for_next_slot, build_empty_block, sign_block
from eth2spec.test.helpers.keys import privkeys, pubkeys
from eth2spec.test.helpers.attester_slashings import get_valid_attester_slashing
from eth2spec.test.helpers.proposer_slashings import get_valid_proposer_slashing
from eth2spec.test.helpers.attestations import get_valid_attestation
from eth2spec.test.helpers.deposits import prepare_state_and_deposit

from eth2spec.test.context import spec_state_test, with_all_phases, expect_assertion_error


@with_all_phases
@spec_state_test
def test_prev_slot_block_transition(spec, state):
    # Go to clean slot
    spec.process_slots(state, state.slot + 1)
    # Make a block for it
    block = build_empty_block(spec, state, slot=state.slot, signed=True)
    # Transition to next slot, above block will not be invalid on top of new state.
    spec.process_slots(state, state.slot + 1)

    yield 'pre', state
    expect_assertion_error(lambda: state_transition_and_sign_block(spec, state, block))
    yield 'blocks', [block]
    yield 'post', None


@with_all_phases
@spec_state_test
def test_same_slot_block_transition(spec, state):
    # Same slot on top of pre-state, but move out of slot 0 first.
    spec.process_slots(state, state.slot + 1)

    block = build_empty_block(spec, state, slot=state.slot, signed=True)

    yield 'pre', state

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state


@with_all_phases
@spec_state_test
def test_empty_block_transition(spec, state):
    pre_slot = state.slot
    pre_eth1_votes = len(state.eth1_data_votes)

    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state, signed=True)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert len(state.eth1_data_votes) == pre_eth1_votes + 1
    assert spec.get_block_root_at_slot(state, pre_slot) == block.parent_root
    assert spec.get_randao_mix(state, spec.get_current_epoch(state)) != spec.Hash()


@with_all_phases
@spec_state_test
def test_invalid_state_root(spec, state):
    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state)
    block.state_root = b"\xaa" * 32
    sign_block(spec, state, block)

    expect_assertion_error(
        lambda: spec.state_transition(state, block, validate_state_root=True))

    yield 'blocks', [block]
    yield 'post', None


@with_all_phases
@spec_state_test
def test_skipped_slots(spec, state):
    pre_slot = state.slot
    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state)
    block.slot += 3
    sign_block(spec, state, block)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert state.slot == block.slot
    assert spec.get_randao_mix(state, spec.get_current_epoch(state)) != spec.Hash()
    for slot in range(pre_slot, state.slot):
        assert spec.get_block_root_at_slot(state, slot) == block.parent_root


@with_all_phases
@spec_state_test
def test_empty_epoch_transition(spec, state):
    pre_slot = state.slot
    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state)
    block.slot += spec.SLOTS_PER_EPOCH
    sign_block(spec, state, block)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert state.slot == block.slot
    for slot in range(pre_slot, state.slot):
        assert spec.get_block_root_at_slot(state, slot) == block.parent_root


@with_all_phases
@spec_state_test
def test_empty_epoch_transition_not_finalizing(spec, state):
    # Don't run for non-minimal configs, it takes very long, and the effect
    # of calling finalization/justification is just the same as with the minimal configuration.
    if spec.SLOTS_PER_EPOCH > 8:
        return

    # copy for later balance lookups.
    pre_balances = list(state.balances)
    yield 'pre', state

    spec.process_slots(state, state.slot + (spec.SLOTS_PER_EPOCH * 5))
    block = build_empty_block_for_next_slot(spec, state, signed=True)
    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert state.slot == block.slot
    assert state.finalized_checkpoint.epoch < spec.get_current_epoch(state) - 4
    for index in range(len(state.validators)):
        assert state.balances[index] < pre_balances[index]


@with_all_phases
@spec_state_test
def test_proposer_slashing(spec, state):
    # copy for later balance lookups.
    pre_state = deepcopy(state)
    proposer_slashing = get_valid_proposer_slashing(spec, state, signed_1=True, signed_2=True)
    validator_index = proposer_slashing.proposer_index

    assert not state.validators[validator_index].slashed

    yield 'pre', state

    #
    # Add to state via block transition
    #
    block = build_empty_block_for_next_slot(spec, state)
    block.body.proposer_slashings.append(proposer_slashing)
    sign_block(spec, state, block)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    # check if slashed
    slashed_validator = state.validators[validator_index]
    assert slashed_validator.slashed
    assert slashed_validator.exit_epoch < spec.FAR_FUTURE_EPOCH
    assert slashed_validator.withdrawable_epoch < spec.FAR_FUTURE_EPOCH
    # lost whistleblower reward
    assert get_balance(state, validator_index) < get_balance(pre_state, validator_index)


@with_all_phases
@spec_state_test
def test_attester_slashing(spec, state):
    # copy for later balance lookups.
    pre_state = deepcopy(state)

    attester_slashing = get_valid_attester_slashing(spec, state, signed_1=True, signed_2=True)
    validator_index = attester_slashing.attestation_1.attesting_indices[0]

    assert not state.validators[validator_index].slashed

    yield 'pre', state

    #
    # Add to state via block transition
    #
    block = build_empty_block_for_next_slot(spec, state)
    block.body.attester_slashings.append(attester_slashing)
    sign_block(spec, state, block)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    slashed_validator = state.validators[validator_index]
    assert slashed_validator.slashed
    assert slashed_validator.exit_epoch < spec.FAR_FUTURE_EPOCH
    assert slashed_validator.withdrawable_epoch < spec.FAR_FUTURE_EPOCH
    # lost whistleblower reward
    assert get_balance(state, validator_index) < get_balance(pre_state, validator_index)

    proposer_index = spec.get_beacon_proposer_index(state)
    # gained whistleblower reward
    assert (
        get_balance(state, proposer_index) >
        get_balance(pre_state, proposer_index)
    )


@with_all_phases
@spec_state_test
def test_expected_deposit_in_block(spec, state):
    # Make the state expect a deposit, then don't provide it.
    state.eth1_data.deposit_count += 1
    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state)
    sign_block(spec, state, block)
    bad = False
    try:
        state_transition_and_sign_block(spec, state, block)
        bad = True
    except AssertionError:
        pass
    if bad:
        raise AssertionError("expected deposit was not enforced")

    yield 'blocks', [block]
    yield 'post', None


@with_all_phases
@spec_state_test
def test_deposit_in_block(spec, state):
    initial_registry_len = len(state.validators)
    initial_balances_len = len(state.balances)

    validator_index = len(state.validators)
    amount = spec.MAX_EFFECTIVE_BALANCE
    deposit = prepare_state_and_deposit(spec, state, validator_index, amount, signed=True)

    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state)
    block.body.deposits.append(deposit)
    sign_block(spec, state, block)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert len(state.validators) == initial_registry_len + 1
    assert len(state.balances) == initial_balances_len + 1
    assert get_balance(state, validator_index) == spec.MAX_EFFECTIVE_BALANCE
    assert state.validators[validator_index].pubkey == pubkeys[validator_index]


@with_all_phases
@spec_state_test
def test_deposit_top_up(spec, state):
    validator_index = 0
    amount = spec.MAX_EFFECTIVE_BALANCE // 4
    deposit = prepare_state_and_deposit(spec, state, validator_index, amount)

    initial_registry_len = len(state.validators)
    initial_balances_len = len(state.balances)
    validator_pre_balance = get_balance(state, validator_index)

    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state)
    block.body.deposits.append(deposit)
    sign_block(spec, state, block)

    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert len(state.validators) == initial_registry_len
    assert len(state.balances) == initial_balances_len
    assert get_balance(state, validator_index) == validator_pre_balance + amount


@with_all_phases
@spec_state_test
def test_attestation(spec, state):
    state.slot = spec.SLOTS_PER_EPOCH

    yield 'pre', state

    attestation = get_valid_attestation(spec, state, signed=True)

    # Add to state via block transition
    pre_current_attestations_len = len(state.current_epoch_attestations)
    attestation_block = build_empty_block_for_next_slot(spec, state)
    attestation_block.slot += spec.MIN_ATTESTATION_INCLUSION_DELAY
    attestation_block.body.attestations.append(attestation)
    sign_block(spec, state, attestation_block)
    state_transition_and_sign_block(spec, state, attestation_block)

    assert len(state.current_epoch_attestations) == pre_current_attestations_len + 1

    # Epoch transition should move to previous_epoch_attestations
    pre_current_attestations_root = spec.hash_tree_root(state.current_epoch_attestations)

    epoch_block = build_empty_block_for_next_slot(spec, state)
    epoch_block.slot += spec.SLOTS_PER_EPOCH
    sign_block(spec, state, epoch_block)
    state_transition_and_sign_block(spec, state, epoch_block)

    yield 'blocks', [attestation_block, epoch_block]
    yield 'post', state

    assert len(state.current_epoch_attestations) == 0
    assert spec.hash_tree_root(state.previous_epoch_attestations) == pre_current_attestations_root


@with_all_phases
@spec_state_test
def test_voluntary_exit(spec, state):
    validator_index = spec.get_active_validator_indices(
        state,
        spec.get_current_epoch(state)
    )[-1]

    # move state forward PERSISTENT_COMMITTEE_PERIOD epochs to allow for exit
    state.slot += spec.PERSISTENT_COMMITTEE_PERIOD * spec.SLOTS_PER_EPOCH

    yield 'pre', state

    voluntary_exit = spec.VoluntaryExit(
        epoch=spec.get_current_epoch(state),
        validator_index=validator_index,
    )
    voluntary_exit.signature = bls_sign(
        message_hash=signing_root(voluntary_exit),
        privkey=privkeys[validator_index],
        domain=spec.get_domain(
            state=state,
            domain_type=spec.DOMAIN_VOLUNTARY_EXIT,
        )
    )

    # Add to state via block transition
    initiate_exit_block = build_empty_block_for_next_slot(spec, state)
    initiate_exit_block.body.voluntary_exits.append(voluntary_exit)
    sign_block(spec, state, initiate_exit_block)
    state_transition_and_sign_block(spec, state, initiate_exit_block)

    assert state.validators[validator_index].exit_epoch < spec.FAR_FUTURE_EPOCH

    # Process within epoch transition
    exit_block = build_empty_block_for_next_slot(spec, state)
    exit_block.slot += spec.SLOTS_PER_EPOCH
    sign_block(spec, state, exit_block)
    state_transition_and_sign_block(spec, state, exit_block)

    yield 'blocks', [initiate_exit_block, exit_block]
    yield 'post', state

    assert state.validators[validator_index].exit_epoch < spec.FAR_FUTURE_EPOCH


@with_all_phases
@spec_state_test
def test_balance_driven_status_transitions(spec, state):
    current_epoch = spec.get_current_epoch(state)
    validator_index = spec.get_active_validator_indices(state, current_epoch)[-1]

    assert state.validators[validator_index].exit_epoch == spec.FAR_FUTURE_EPOCH

    # set validator balance to below ejection threshold
    state.validators[validator_index].effective_balance = spec.EJECTION_BALANCE

    yield 'pre', state

    # trigger epoch transition
    block = build_empty_block_for_next_slot(spec, state)
    block.slot += spec.SLOTS_PER_EPOCH
    sign_block(spec, state, block)
    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert state.validators[validator_index].exit_epoch < spec.FAR_FUTURE_EPOCH


@with_all_phases
@spec_state_test
def test_historical_batch(spec, state):
    state.slot += spec.SLOTS_PER_HISTORICAL_ROOT - (state.slot % spec.SLOTS_PER_HISTORICAL_ROOT) - 1
    pre_historical_roots_len = len(state.historical_roots)

    yield 'pre', state

    block = build_empty_block_for_next_slot(spec, state, signed=True)
    sign_block(spec, state, block)
    state_transition_and_sign_block(spec, state, block)

    yield 'blocks', [block]
    yield 'post', state

    assert state.slot == block.slot
    assert spec.get_current_epoch(state) % (spec.SLOTS_PER_HISTORICAL_ROOT // spec.SLOTS_PER_EPOCH) == 0
    assert len(state.historical_roots) == pre_historical_roots_len + 1


@with_all_phases
@spec_state_test
def test_eth1_data_votes_consensus(spec, state):
    # Don't run when it will take very, very long to simulate. Minimal configuration suffices.
    if spec.SLOTS_PER_ETH1_VOTING_PERIOD > 16:
        return

    offset_block = build_empty_block(spec, state, slot=spec.SLOTS_PER_ETH1_VOTING_PERIOD - 1)
    sign_block(spec, state, offset_block)
    state_transition_and_sign_block(spec, state, offset_block)
    yield 'pre', state

    a = b'\xaa' * 32
    b = b'\xbb' * 32
    c = b'\xcc' * 32

    blocks = []

    for i in range(0, spec.SLOTS_PER_ETH1_VOTING_PERIOD):
        block = build_empty_block_for_next_slot(spec, state)
        # wait for over 50% for A, then start voting B
        block.body.eth1_data.block_hash = b if i * 2 > spec.SLOTS_PER_ETH1_VOTING_PERIOD else a
        sign_block(spec, state, block)
        state_transition_and_sign_block(spec, state, block)
        blocks.append(block)

    assert len(state.eth1_data_votes) == spec.SLOTS_PER_ETH1_VOTING_PERIOD
    assert state.eth1_data.block_hash == a

    # transition to next eth1 voting period
    block = build_empty_block_for_next_slot(spec, state)
    block.body.eth1_data.block_hash = c
    sign_block(spec, state, block)
    state_transition_and_sign_block(spec, state, block)
    blocks.append(block)

    yield 'blocks', blocks
    yield 'post', state

    assert state.eth1_data.block_hash == a
    assert state.slot % spec.SLOTS_PER_ETH1_VOTING_PERIOD == 0
    assert len(state.eth1_data_votes) == 1
    assert state.eth1_data_votes[0].block_hash == c


@with_all_phases
@spec_state_test
def test_eth1_data_votes_no_consensus(spec, state):
    # Don't run when it will take very, very long to simulate. Minimal configuration suffices.
    if spec.SLOTS_PER_ETH1_VOTING_PERIOD > 16:
        return

    pre_eth1_hash = state.eth1_data.block_hash

    offset_block = build_empty_block(spec, state, slot=spec.SLOTS_PER_ETH1_VOTING_PERIOD - 1)
    sign_block(spec, state, offset_block)
    state_transition_and_sign_block(spec, state, offset_block)
    yield 'pre', state

    a = b'\xaa' * 32
    b = b'\xbb' * 32

    blocks = []

    for i in range(0, spec.SLOTS_PER_ETH1_VOTING_PERIOD):
        block = build_empty_block_for_next_slot(spec, state)
        # wait for precisely 50% for A, then start voting B for other 50%
        block.body.eth1_data.block_hash = b if i * 2 >= spec.SLOTS_PER_ETH1_VOTING_PERIOD else a
        sign_block(spec, state, block)
        state_transition_and_sign_block(spec, state, block)
        blocks.append(block)

    assert len(state.eth1_data_votes) == spec.SLOTS_PER_ETH1_VOTING_PERIOD
    assert state.eth1_data.block_hash == pre_eth1_hash

    yield 'blocks', blocks
    yield 'post', state
