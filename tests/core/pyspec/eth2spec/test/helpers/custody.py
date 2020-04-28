from eth2spec.test.helpers.keys import privkeys
from eth2spec.utils import bls
from eth2spec.utils.ssz.ssz_typing import Bitlist, ByteVector, Bitvector, ByteList, uint64
from eth2spec.utils.ssz.ssz_impl import hash_tree_root
from eth2spec.utils.merkle_minimal import get_merkle_root, get_merkle_tree, get_merkle_proof
from remerkleable.core import pack_bits_to_chunks
from remerkleable.tree import subtree_fill_to_contents, get_depth, Node, Gindex, gindex_bit_iter, Root

BYTES_PER_CHUNK = 32


def get_valid_early_derived_secret_reveal(spec, state, epoch=None):
    current_epoch = spec.get_current_epoch(state)
    revealed_index = spec.get_active_validator_indices(state, current_epoch)[-1]
    masker_index = spec.get_active_validator_indices(state, current_epoch)[0]

    if epoch is None:
        epoch = current_epoch + spec.CUSTODY_PERIOD_TO_RANDAO_PADDING

    # Generate the secret that is being revealed
    domain = spec.get_domain(state, spec.DOMAIN_RANDAO, epoch)
    signing_root = spec.compute_signing_root(spec.Epoch(epoch), domain)
    reveal = bls.Sign(privkeys[revealed_index], signing_root)
    # Generate the mask (any random 32 bytes that don't reveal the masker's secret will do)
    mask = spec.hash(reveal)
    # Generate masker's signature on the mask
    signing_root = spec.compute_signing_root(mask, domain)
    masker_signature = bls.Sign(privkeys[masker_index], signing_root)
    masked_reveal = bls.Aggregate([reveal, masker_signature])

    return spec.EarlyDerivedSecretReveal(
        revealed_index=revealed_index,
        epoch=epoch,
        reveal=masked_reveal,
        masker_index=masker_index,
        mask=mask,
    )


def get_valid_custody_key_reveal(spec, state, period=None, validator_index=None):
    current_epoch = spec.get_current_epoch(state)
    revealer_index = (spec.get_active_validator_indices(state, current_epoch)[0]
                      if validator_index is None else validator_index)
    revealer = state.validators[revealer_index]

    if period is None:
        period = revealer.next_custody_secret_to_reveal

    epoch_to_sign = spec.get_randao_epoch_for_custody_period(period, revealer_index)

    # Generate the secret that is being revealed
    domain = spec.get_domain(state, spec.DOMAIN_RANDAO, epoch_to_sign)
    signing_root = spec.compute_signing_root(spec.Epoch(epoch_to_sign), domain)
    reveal = bls.Sign(privkeys[revealer_index], signing_root)
    return spec.CustodyKeyReveal(
        revealer_index=revealer_index,
        reveal=reveal,
    )


def bitlist_from_int(max_len, num_bits, n):
    return Bitlist[max_len](*[(n >> i) & 0b1 for i in range(num_bits)])


def get_valid_custody_slashing(spec, state, attestation, shard_transition, invalid_custody_bit=False):
    beacon_committee = spec.get_beacon_committee(
        state,
        attestation.data.slot,
        attestation.data.index,
    )
    malefactor_index = beacon_committee[0]
    whistleblower_index = beacon_committee[-1]

    epoch = spec.get_randao_epoch_for_custody_period(attestation.data.target.epoch,
                                                     malefactor_index)

    # Generate the responder key
    domain = spec.get_domain(state, spec.DOMAIN_RANDAO, epoch)
    signing_root = spec.compute_signing_root(spec.Epoch(epoch), domain)
    malefactor_key = bls.Sign(privkeys[malefactor_index], signing_root)
    data_index = 0
    data=ByteList[spec.MAX_SHARD_BLOCK_SIZE](get_custody_test_vector(shard_transition.shard_block_lengths[data_index]))
    print(hash_tree_root(data))
    print(data.get_backing().get_left().merkle_root())

    slashing = spec.CustodySlashing(
        data_index=data_index,
        malefactor_index=malefactor_index,
        malefactor_secret=malefactor_key,
        whistleblower_index=whistleblower_index,
        shard_transition=shard_transition,
        attestation=attestation,
        data=data,
    )
    slashing_domain = spec.get_domain(state, spec.DOMAIN_CUSTODY_BIT_SLASHING)
    slashing_root = spec.compute_signing_root(slashing, domain)

    signed_slashing = spec.SignedCustodySlashing(
        message=slashing,
        signature=bls.Sign(privkeys[whistleblower_index], slashing_root)
    )

    return signed_slashing


def get_valid_chunk_challenge(spec, state, attestation, shard_transition):
    shard = spec.compute_shard_from_committee_index(state, attestation.data.index, attestation.data.slot)
    crosslink_committee = spec.get_beacon_committee(
        state,
        attestation.data.slot,
        attestation.data.index
    )
    responder_index = crosslink_committee[0]
    data_index = len(shard_transition.shard_block_lengths) - 1

    chunk_count = (shard_transition.shard_block_lengths[data_index] + spec.BYTES_PER_CUSTODY_CHUNK - 1) // spec.BYTES_PER_CUSTODY_CHUNK

    return spec.CustodyChunkChallenge(
        responder_index=responder_index,
        attestation=attestation,
        chunk_index=chunk_count - 1,
        data_index=data_index,
        shard_transition=shard_transition,
    )


def custody_chunkify(spec, x):
    chunks = [bytes(x[i:i + spec.BYTES_PER_CUSTODY_CHUNK]) for i in range(0, len(x), spec.BYTES_PER_CUSTODY_CHUNK)]
    chunks[-1] = chunks[-1].ljust(spec.BYTES_PER_CUSTODY_CHUNK, b"\0")
    return chunks


def build_proof(anchor, leaf_index):
    if leaf_index <= 1:
        return []  # Nothing to prove / invalid index
    node = anchor
    proof = []
    # Walk down, top to bottom to the leaf
    bit_iter, _ = gindex_bit_iter(leaf_index)
    for bit in bit_iter:
        # Always take the opposite hand for the proof.
        # 1 = right as leaf, thus get left
        if bit:
            proof.append(node.get_left().merkle_root())
            node = node.get_right()
        else:
            proof.append(node.get_right().merkle_root())
            node = node.get_left()

    return list(reversed(proof))


def get_valid_custody_chunk_response(spec, state, chunk_challenge, block_length, challenge_index,
                                     invalid_chunk_data=False):
    custody_data = get_custody_test_vector(block_length)
    custody_data_block = ByteList[spec.MAX_SHARD_BLOCK_SIZE](custody_data)
    chunks = custody_chunkify(spec, custody_data)

    chunk_index = chunk_challenge.chunk_index

    data_branch = build_proof(custody_data_block.get_backing().get_left(), chunk_index + 2**spec.CUSTODY_RESPONSE_DEPTH)

    return spec.CustodyChunkResponse(
        challenge_index=challenge_index,
        chunk_index=chunk_index,
        chunk=ByteVector[spec.BYTES_PER_CUSTODY_CHUNK](chunks[chunk_index]),
        branch=data_branch,
    )


def get_custody_test_vector(bytelength):
    ints = bytelength // 4 + 1
    return (b"".join(i.to_bytes(4, "little") for i in range(ints)))[:bytelength]


def get_shard_transition(spec, start_slot, block_lengths):
    b = [ByteList[spec.MAX_SHARD_BLOCK_SIZE](get_custody_test_vector(x)).get_backing().get_left().merkle_root() for x in block_lengths]
    shard_transition = spec.ShardTransition(
        start_slot=start_slot,
        shard_block_lengths=block_lengths,
        shard_data_roots=b,
        shard_states=[spec.Root() for x in block_lengths],
        proposer_signature_aggregate=spec.BLSSignature(),
    )
    return shard_transition


def get_custody_merkle_root(data):
    return None  # get_merkle_tree(chunkify(data))[-1][0]
