import logging
import random
from typing import Dict, Optional, List

from src.consensus.constants import ConsensusConstants
from src.consensus.pot_iterations import (
    calculate_iterations_quality,
    calculate_ip_iters,
)
from src.full_node.sub_block_record import SubBlockRecord
from src.types.classgroup import ClassgroupElement
from src.types.full_block import FullBlock
from src.types.header_block import HeaderBlock
from src.types.reward_chain_sub_block import RewardChainSubBlock
from src.types.sized_bytes import bytes32
from src.types.slots import ChallengeChainSubSlot, RewardChainSubSlot
from src.types.sub_epoch_summary import SubEpochSummary
from src.types.vdf import VDFProof, VDFInfo
from src.types.weight_proof import (
    WeightProof,
    SubEpochData,
    SubEpochChallengeSegment,
    SubSlotData,
)
from src.util.hash import std_hash
from src.util.ints import uint32, uint64, uint8
from src.util.vdf_prover import get_vdf_info_and_proof


log = logging.getLogger(__name__)


def full_block_to_header(block: FullBlock) -> HeaderBlock:
    return HeaderBlock(
        block.finished_sub_slots,
        block.reward_chain_sub_block,
        block.challenge_chain_sp_proof,
        block.challenge_chain_ip_proof,
        block.reward_chain_sp_proof,
        block.reward_chain_ip_proof,
        block.infused_challenge_chain_ip_proof,
        block.foliage_sub_block,
        block.foliage_block,
        b"",  # No filter
    )


def combine_proofs(proofs: List[VDFProof]) -> VDFProof:
    # todo

    return VDFProof(witness_type=uint8(0), witness=b"")


def make_sub_epoch_data(
    sub_epoch_summary: SubEpochSummary,
) -> SubEpochData:
    reward_chain_hash: bytes32 = sub_epoch_summary.reward_chain_hash
    #  Number of subblocks overflow in previous slot
    previous_sub_epoch_overflows: uint8 = sub_epoch_summary.num_sub_blocks_overflow  # total in sub epoch - expected
    #  New work difficulty and iterations per subslot
    sub_slot_iters: Optional[uint64] = sub_epoch_summary.new_sub_slot_iters
    new_difficulty: Optional[uint64] = sub_epoch_summary.new_difficulty
    return SubEpochData(reward_chain_hash, previous_sub_epoch_overflows, sub_slot_iters, new_difficulty)


def get_sub_epoch_block_num(block: SubBlockRecord, sub_blocks: Dict[bytes32, SubBlockRecord]) -> uint32:
    """
    returns the number of blocks in a sub epoch ending with
    """
    curr = block
    count: uint32 = uint32(0)
    while not curr.sub_epoch_summary_included:
        # todo skip overflows from last sub epoch
        if curr.height == 0:
            return count

        curr = sub_blocks[curr.prev_hash]
        count += 1

    count += 1
    return count


def choose_sub_epoch(sub_epoch_blocks_n: uint32, rng: random.Random, total_number_of_blocks: uint64) -> bool:
    prob = sub_epoch_blocks_n / total_number_of_blocks
    for i in range(sub_epoch_blocks_n):
        if rng.random() < prob:
            return True
    return False


# returns a challenge chain vdf from slot start to signage point
def first_sub_slots_data(
    block: HeaderBlock, header_cache: Dict[bytes32, HeaderBlock], height_to_hash: Dict[uint32, bytes32]
) -> (List[SubSlotData], uint64):
    # combine cc vdfs of all reward blocks from the start of the sub slot to end

    sub_slots: List[SubSlotData] = []
    # challenge block Proof of space
    proof_of_space = block.reward_chain_sub_block.proof_of_space
    # challenge block Signature of signage point
    cc_signage_point_sig = block.reward_chain_sub_block.challenge_chain_sp_signature

    # todo vdf of the overflow blocks before the challenge block ?

    sub_slots: List[SubSlotData]
    # get all finished sub slots
    if len(block.finished_sub_slots) > 0:
        for sub_slot in block.finished_sub_slots:
            sub_slots.append(
                SubSlotData(
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    icc_slot_vdf=sub_slot.proofs.infused_challenge_chain_slot_proof,
                    cc_slot_vdf=sub_slot.proofs.challenge_chain_slot_proof,
                )
            )

    # find sub slot end
    curr = block
    cc_slot_end_vdf: List[VDFProof] = []
    icc_slot_end_vdf: List[VDFProof] = []
    while True:
        curr = header_cache[height_to_hash[curr.height + 1]]
        if len(curr.finished_sub_slots) > 0:
            # sub slot ended
            next_slot_height = curr.height + 1
            break

        cc_slot_end_vdf.extend([curr.challenge_chain_sp_proof, curr.challenge_chain_ip_proof])
        icc_slot_end_vdf.append(curr.infused_challenge_chain_ip_proof)
    sub_slots.append(
        SubSlotData(
            proof_of_space,
            cc_signage_point_sig,
            block.challenge_chain_sp_proof,
            block.challenge_chain_ip_proof,
            combine_proofs(cc_slot_end_vdf),
            combine_proofs(icc_slot_end_vdf),
            block.reward_chain_sub_block.signage_point_index,
            None,
            None,
        )
    )

    return sub_slots, next_slot_height


# returns a challenge chain vdf from infusion point to end of slot
def get_slot_end_vdf(
    constants: ConsensusConstants,
    block: HeaderBlock,
    height_to_hash: Dict[uint32, bytes32],
    header_cache: Dict[bytes32, HeaderBlock],
) -> List[SubSlotData]:
    # get all vdfs first sub slot after challenge block to last sub slot
    curr = block
    cc_proofs: List[VDFProof] = []
    icc_proofs: List[VDFProof] = []
    sub_slots_data: List[SubSlotData] = []

    while curr is not None:
        curr = header_cache[height_to_hash[curr.height + 1]]
        if len(curr.finished_sub_slots) > 0:
            # slot finished combine proofs and add slot data to list
            sub_slots_data.append(
                SubSlotData(
                    None, None, None, None, combine_proofs(cc_proofs), combine_proofs(icc_proofs), None, None, None
                )
            )

            # handle finished empty sub slots
            for sub_slot in curr.finished_sub_slots:
                data = SubSlotData(
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    sub_slot.proofs.infused_challenge_chain_slot_proof,
                    sub_slot.proofs.challenge_chain_slot_proof,
                )
                sub_slots_data.append(data)
                if sub_slot.reward_chain.deficit == constants.MIN_SUB_BLOCKS_PER_CHALLENGE_BLOCK:
                    # end of challenge slot
                    break

            # new sub slot
            cc_proofs = []

        # append sub slot proofs
        icc_proofs.append(curr.infused_challenge_chain_ip_proof)
        cc_proofs.extend([curr.challenge_chain_sp_proof, curr.challenge_chain_ip_proof])
        print("block", curr.height + 1, " ", len(height_to_hash))

    return sub_slots_data


def handle_challenge_segment(
    constants: ConsensusConstants,
    block: HeaderBlock,
    sub_epoch_n: uint32,
    height_to_hash: Dict[uint32, bytes32],
    header_cache: Dict[bytes32, HeaderBlock],
) -> SubEpochChallengeSegment:
    sub_slots: List[SubSlotData] = []
    log.info(f"create challenge segment for block {block.header_hash} height {block.height} ")

    # VDFs from sub slots before challenge block
    log.info(f"create ip vdf for block {block.header_hash} height {block.height} ")
    first_sub_slots, end_height = first_sub_slots_data(block, header_cache, height_to_hash)
    sub_slots.extend(first_sub_slots)

    # VDFs from slot after challenge block to end of slot
    log.info(f"create slot end vdf for block {block.header_hash} height {block.height} ")
    challenge_slot_end_sub_slots = get_slot_end_vdf(
        constants, header_cache[height_to_hash[end_height]], height_to_hash, header_cache
    )
    sub_slots.extend(challenge_slot_end_sub_slots)
    return SubEpochChallengeSegment(sub_epoch_n, block.reward_chain_sub_block.reward_chain_ip_vdf, sub_slots)


def create_sub_epoch_segments(
    constants: ConsensusConstants,
    block: SubBlockRecord,
    sub_epoch_blocks_n: uint32,
    sub_blocks: Dict[bytes32, SubBlockRecord],
    sub_epoch_n: uint32,
    header_cache: Dict[bytes32, HeaderBlock],
    height_to_hash: Dict[uint32, bytes32],
) -> List[SubEpochChallengeSegment]:
    """
    receives the last block in sub epoch and creates List[SubEpochChallengeSegment] for that sub_epoch
    """

    segments: List[SubEpochChallengeSegment] = []
    curr = block

    count = sub_epoch_blocks_n
    while not count == 0:
        log.info(f"challenge segment, starts at {curr.height} ")
        curr = sub_blocks[curr.prev_hash]
        count -= 1
        if not curr.is_challenge_sub_block(constants):
            continue

        challebge_sub_block = header_cache[curr.header_hash]
        segment = handle_challenge_segment(constants, challebge_sub_block, sub_epoch_n, height_to_hash, header_cache)
        segments.append(segment)

    return segments


def make_weight_proof(
    constants: ConsensusConstants,
    recent_blocks_n: uint32,
    tip: bytes32,
    sub_blocks: Dict[bytes32, SubBlockRecord],
    total_number_of_blocks: uint64,
    header_cache: Dict[bytes32, HeaderBlock],
    height_to_hash: Dict[uint32, bytes32],
) -> WeightProof:
    """
    Creates a weight proof object
    """
    sub_epoch_data: List[SubEpochData] = []
    sub_epoch_segments: List[SubEpochChallengeSegment] = []
    proof_blocks: List[RewardChainSubBlock] = []
    curr: SubBlockRecord = sub_blocks[tip]
    rng: random.Random = random.Random(tip)
    # ses_hash from the latest sub epoch summary before this part of the chain
    log.info(f"build weight proofs peak : {sub_blocks[tip].header_hash } num of blocks: {total_number_of_blocks}")
    assert sub_blocks[tip].height > total_number_of_blocks
    sub_epoch_n = count_sub_epochs_in_range(curr, sub_blocks, total_number_of_blocks)
    sub_epoch_idx: uint32 = uint32(0)
    while not total_number_of_blocks == 0:
        assert curr.height != 0

        # next sub block
        curr = sub_blocks[curr.prev_hash]
        header_block = header_cache[curr.header_hash]
        # for each sub-epoch
        if curr.sub_epoch_summary_included is not None:
            sub_epoch_data.append(make_sub_epoch_data(curr.sub_epoch_summary_included))
            # get sub_epoch_blocks_n in sub_epoch
            sub_epoch_blocks_n = get_sub_epoch_block_num(curr, sub_blocks)
            #   sample sub epoch
            if choose_sub_epoch(sub_epoch_blocks_n, rng, total_number_of_blocks):
                sub_epoch_segments = create_sub_epoch_segments(
                    constants,
                    curr,
                    sub_epoch_blocks_n,
                    sub_blocks,
                    sub_epoch_n - sub_epoch_idx,
                    header_cache,
                    height_to_hash,
                )
            sub_epoch_idx += 1

        if recent_blocks_n > 0:
            # add to needed reward chain recent blocks
            proof_blocks.append(header_block.reward_chain_sub_block)
            recent_blocks_n -= 1

        total_number_of_blocks -= 1

    return WeightProof(sub_epoch_data, sub_epoch_segments, proof_blocks)


def count_sub_epochs_in_range(curr, sub_blocks, total_number_of_blocks):
    sub_epochs_n = 0
    while not total_number_of_blocks == 0:
        assert curr.height != 0
        curr = sub_blocks[curr.prev_hash]
        if curr.sub_epoch_summary_included is not None:
            sub_epochs_n += 1
        total_number_of_blocks -= 1
    return sub_epochs_n


def validate_sub_slot_vdfs(
    constants: ConsensusConstants, sub_slot: SubSlotData, vdf_info: VDFInfo, infused: bool
) -> bool:
    if infused:
        if not sub_slot.cc_signage_point_vdf.is_valid(constants, vdf_info):
            return False

        if not sub_slot.cc_infusion_point_vdf.is_valid(constants, vdf_info):
            return False

        if not sub_slot.cc_infusion_to_slot_end_vdf.is_valid(constants, vdf_info):
            return False

        if not sub_slot.icc_infusion_to_slot_end_vdf.is_valid(constants, vdf_info):
            return False
        return True

    return sub_slot.cc_slot_vdf.is_valid(constants, vdf_info)


def validate_weight(
    constants: ConsensusConstants,
    weight_proof: WeightProof,
    prev_ses_hash: bytes32,
) -> bool:
    # sub epoch summaries validate hashes
    summaries, sub_epoch_data_weight = map_summaries(constants, prev_ses_hash, weight_proof)

    # last ses
    ses = summaries[len(summaries)]

    # find first block after last sub epoch end
    count, block_idx = 0, 0
    for idx, block in enumerate(weight_proof.recent_reward_chain):
        if finishes_sub_epoch(constants, weight_proof.recent_reward_chain, idx):
            block_idx = count

        count += 1

    # validate weight
    last_sub_epoch_weight = weight_proof.recent_reward_chain[block_idx].weight - ses.new_difficulty
    if last_sub_epoch_weight != sub_epoch_data_weight:
        log.error(f"failed to validate weight got {sub_epoch_data_weight} expected {last_sub_epoch_weight}")
        return False

    # validate last ses_hash
    cc_vdf = weight_proof.recent_reward_chain[block_idx - 1].challenge_chain_ip_vdf
    challenge = std_hash(ChallengeChainSubSlot(cc_vdf, None, std_hash(ses), ses.new_ips, ses.new_difficulty))
    if challenge != weight_proof.recent_reward_chain[block_idx + 1].challenge_chain_sp_vdf.challenge:
        log.error("failed to validate ses hashes")
        return False

    total_ip_iters = uint64(0)
    total_challenge_blocks = 0
    total_slot_iters = uint64(0)
    total_slots = 0
    # validate sub epoch samples
    for segment in weight_proof.sub_epoch_segments:
        ses = summaries[segment.sub_epoch_n]
        ssi: uint64 = uint64(constants.SUB_SLOT_TIME_TARGET * ses.new_ips)
        total_slot_iters += ssi
        q_str = validate_proof_of_space(constants, segment, summaries, ssi)
        if q_str is None:
            return False

        # validate vdfs
        challenge = ses.prev_subepoch_summary_hash
        for sub_slot in segment.sub_slots:
            total_slots += 1
            vdf_info, _ = get_vdf_info_and_proof(constants, ClassgroupElement.get_default_element(), challenge, ssi)
            if not validate_sub_slot_vdfs(constants, sub_slot, vdf_info, sub_slot.is_challenge()):
                log.error("failed to validate vdfs for sub slot ")
                return False

            if sub_slot.is_challenge():
                required_iters: uint64 = calculate_iterations_quality(
                    q_str,
                    sub_slot.proof_of_space.size,
                    challenge,
                    sub_slot.cc_signage_point_vdf.get_hash(),
                )
                total_ip_iters = +calculate_ip_iters(
                    constants, ses.new_ips, sub_slot.cc_signage_point_index, required_iters
                )
                total_challenge_blocks += 1
                challenge = sub_slot.cc_slot_vdf.get_hash()
            else:
                challenge = sub_slot.cc_infusion_to_slot_end_vdf.get_hash()

                # validate reward chain sub slot obj with next ses
                rc_sub_slot = RewardChainSubSlot(
                    segment.last_reward_chain_vdf_info,
                    segment.sub_slots[-1].cc_infusion_to_slot_end_vdf.get_hash(),
                    segment.sub_slots[-1].icc_infusion_to_slot_end_vdf.get_hash(),
                    uint8(0),
                )

                if not summaries[segment.sub_epoch_n + 1].reward_chain_hash == rc_sub_slot.get_hash():
                    log.error(f"segment {segment.sub_epoch_n} failed reward_chain_hash validation")
                    return False

        avg_ip_iters = total_ip_iters / total_challenge_blocks
        avg_slot_iters = total_slot_iters / total_slots
        if avg_ip_iters / avg_slot_iters > constants.WEIGHT_PROOF_THRESHOLD:
            return False

    # validate recent reward chain

    return True


def validate_proof_of_space(constants, segment, summaries, slot_iters):
    # find challenge block sub slot
    challenge_sub_slot = None
    for slot in segment.sub_slots:
        if slot.proof_of_space is not None:
            challenge_sub_slot = slot
            break
    # get summary
    ses = summaries[segment.sub_epoch_n]
    cc_vdf, _ = get_vdf_info_and_proof(constants, ClassgroupElement.get_default_element(), std_hash(ses), slot_iters)
    # get challenge
    challenge = std_hash(
        ChallengeChainSubSlot(cc_vdf, None, ses.prev_subepoch_summary_hash, ses.new_ips, ses.new_difficulty)
    )
    # validate proof of space
    q_str: Optional[bytes32] = challenge_sub_slot.proof_of_space.verify_and_get_quality_string(
        constants,
        challenge,
        challenge_sub_slot.cc_signage_point_sig,
    )
    return q_str


def map_summaries(constants, ses_hash, weight_proof):
    sub_epoch_data_weight: uint64 = uint64(0)
    summaries: Dict[uint32, SubEpochSummary] = {}
    for idx, sub_epoch_data in enumerate(weight_proof.sub_epochs):
        ses = SubEpochSummary(
            ses_hash,
            sub_epoch_data.reward_chain_hash,
            sub_epoch_data.previous_sub_epoch_overflows,
            sub_epoch_data.new_difficulty,
            sub_epoch_data.sub_slot_iters,
        )

        sub_epoch_data_weight += sub_epoch_data.new_difficulty * (
            constants.SUB_EPOCH_SUB_BLOCKS + sub_epoch_data.num_sub_blocks_overflow
        )

        # add to dict
        summaries[idx] = ses
        ses_hash = std_hash(ses)
    return summaries, sub_epoch_data_weight


def finishes_sub_epoch(constants: ConsensusConstants, blocks: List[RewardChainSubBlock], index: int) -> bool:
    return True
