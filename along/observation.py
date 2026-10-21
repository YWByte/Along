"""Isolated cross-frame observation, without event summaries or prior assistant verdicts."""

from along.providers.vlm import ImageInput, ProviderError
from along.scenarios import skill
from along.schemas import AnswerAudit, VisualReview
from along.store import dump


def resolve_tiles(refs, frame_ids):
    """Models cite sheet tiles far more reliably by number than by 36-char UUID."""
    resolved = []
    for ref in refs:
        digits = "".join(ch for ch in str(ref) if ch.isdigit())
        if ref in frame_ids:
            resolved.append(ref)
        elif digits and str(ref).strip("#格第 ").isdigit() and 1 <= int(digits) <= len(frame_ids):
            resolved.append(frame_ids[int(digits) - 1])
        else:
            resolved.append(ref)
    return resolved


async def observe_sequence(vlm, scenario, focus, images):
    if len(images) < 2 and not (images and len(images[0].frame_ids) >= 2):
        return VisualReview(
            visibility="unusable", summary="可比较的图片不足，不能判断过程。", limitations=["不足两个时刻"]
        )
    # Capture UI labels and previous assistant judgments are deliberately excluded.
    if len(images) == 1 and len(images[0].frame_ids) >= 2:
        sheet = images[0]
        neutral_images = [ImageInput(sheet.id, sheet.path, sheet.label, sheet.frame_ids)]
        order = {fid: i for i, fid in enumerate(sheet.frame_ids)}
    else:
        neutral_images = [
            ImageInput(im.id, im.path, f"片段顺序 {i + 1}；{'；'.join(im.label.split('；')[:2])}")
            for i, im in enumerate(images)
        ]
        order = {image.id: i for i, image in enumerate(images)}
    result = await vlm.structured(
        "只做视觉观察，不判断用户是否做对、不生成操作建议。用户想做的事不是已经发生的事实。"
        "比较按时间排列的原图或拼图（拼图每格有序号和相对秒数，按从左到右、从上到下阅读，引用格对应的帧ID），"
        "片段里可能混有用户打字、拿设备、离开画面等与任务无关的间隙（多在提问前的末尾）；先在拼图中找出执行该步骤的连续格，只依据这些格判断，不因无关格降低结论。"
        "先追踪连续多格中同一部位的运动方向与往复，再选择2至4组不同时间的画面，描述同一对象相对邻近参照物的变化。"
        "相似的两个端点不证明期间静止；不要只给每张图命名，要明确相对位置/朝向/形态的差异。"
        'changes 每项至少引用两个不同原图ID（拼图可直接写格子序号如 "3"），按时间排序；不必凑数量，没有可见变化时为空。'
        "summary 只概括可见内容，不写‘正确/错误/安全/完成’。visibility 是可见性，不是正确率。"
        "有歧义用 limited，根本看不到目标用 unusable。无法确认的运动方向不编造。"
        "needs_detail 仅当缩短时间范围、查看更密集的已有帧可能解决具体歧义时为true。"
        + skill(scenario)["observation"]
        + "\n"
        + dump({"attention_target_not_fact": focus, "frame_count": len(order)}),
        VisualReview,
        neutral_images,
    )
    if len(images) == 1 and images[0].frame_ids:
        for change in result.changes:
            change.frame_ids = resolve_tiles(change.frame_ids, images[0].frame_ids)
    for change in result.changes:
        if (
            not set(change.frame_ids) <= order.keys()
            or len(set(change.frame_ids)) != len(change.frame_ids)
            or change.frame_ids != sorted(change.frame_ids, key=lambda fid: order.get(fid, -1))
        ):
            raise ProviderError("视觉观察引用不属于当前片段、重复或时间反向的图片，未采用")
    return result


async def audit_answer(vlm, scenario, focus, answer, images, reviews, criteria=None):
    result = await vlm.structured(
        "审查一段准备发给用户的回答是否被视觉证据支持，只检查，不增加新的任务要求。"
        "这是弱视觉证据审查，不是动作或安全认证；观察报告也可能错误，须核对原图。"
        "回答可以并且应该对 look_for 中每条可见标准给出 met/not_met/unseen：原图清楚显示满足就是 met，"
        "清楚显示违背就是 not_met，看不到就是 unseen。这类基于画面的判断是允许的，不要删除。"
        "verdict：有 not_met 为 adjust；有 met、其余仅 unseen 为 ok；关键标准全部 unseen 为 unclear。"
        "不要只因速度、力度、放松或次数看不到就把整体降为 unclear，这些不是视觉标准。"
        "多格中可见的往复/朝向变化与标准方向一致即可 met，不要求认证细节；草稿据此给出的 met 不应改为 unseen。"
        "片段里可能混有用户打字、拿设备、离开画面等与任务无关的间隙（多在提问前的末尾）；先在拼图中找出执行该步骤的连续格，只依据这些格判断，不因无关格降低结论。"
        "需要修正的是：判断与原图不符、把 unseen 说成 met 或 not_met、"
        "仅依据稀疏图片断言精确次数、速度、发力、痛感、内部连接牢固或设备可以安全通电、看不清推导成做错。"
        "verdict=adjust 时保留一个针对 not_met 标准、可立即执行的调整建议；其他情况不新增建议。"
        "检查回答是否针对讨论目标，而非误用其他步骤。不要增加检查问卷。"
        "若需修正 action=revise，给出 corrected_reply(1至2句)、corrected_headline(不超过30字)、"
        "corrected_verdict、corrected_criteria、corrected_connection_checks（接线场景逐条ID/状态/帧）、corrected_suggestion；evidence_ids 只引用本次提供的原图或拼图帧ID。否则keep。"
        + skill(scenario)["guidance"]
        + "\n"
        + dump(
            {
                "focus": focus,
                "look_for": criteria or [],
                "draft": answer.model_dump(exclude={"memory", "research"}),
                "observations_not_facts": reviews,
            }
        ),
        AnswerAudit,
        images,
    )
    sheets = [im.frame_ids for im in images if im.frame_ids]
    if len(sheets) == 1:
        result.evidence_ids = resolve_tiles(result.evidence_ids, sheets[0])
        for item in [*result.corrected_criteria, *result.corrected_connection_checks]:
            item.frame_ids = resolve_tiles(item.frame_ids, sheets[0])
    allowed = {fid for im in images for fid in (im.id, *im.frame_ids)}
    if not set(result.evidence_ids) <= allowed:
        raise ProviderError("回答审查引用未提供的图片，未采用")
    return result
