"""Isolated cross-frame observation, without event summaries or prior assistant verdicts."""

from along.providers.vlm import ImageInput, ProviderError
from along.scenarios import skill
from along.schemas import AnswerAudit, VisualReview
from along.store import dump


async def observe_sequence(vlm, scenario, focus, images):
    if len(images) < 2:
        return VisualReview(
            visibility="unusable", summary="可比较的图片不足，不能判断过程。", limitations=["不足两个时刻"]
        )
    # Capture UI labels and previous assistant judgments are deliberately excluded.
    neutral_images = [
        ImageInput(im.id, im.path, f"片段顺序 {i + 1}；{'；'.join(im.label.split('；')[:2])}")
        for i, im in enumerate(images)
    ]
    result = await vlm.structured(
        "只做视觉观察，不判断用户是否做对、不生成操作建议。用户想做的事不是已经发生的事实。"
        "比较按时间排列的原图，选择2至4组不同时间的图片，描述同一对象相对邻近参照物的变化。"
        "相似的两个端点不证明期间静止；不要只给每张图命名，要明确相对位置/朝向/形态的差异。"
        "changes 每项至少引用两个不同原图ID，按时间排序；不必凑数量，没有可见变化时为空。"
        "summary 只概括可见内容，不写‘正确/错误/安全/完成’。visibility 是可见性，不是正确率。"
        "有歧义用 limited，根本看不到目标用 unusable。无法确认的运动方向不编造。"
        "needs_detail 仅当缩短时间范围、查看更密集的已有帧可能解决具体歧义时为true。"
        + skill(scenario)["observation"]
        + "\n"
        + dump({"attention_target_not_fact": focus, "frame_count": len(images)}),
        VisualReview,
        neutral_images,
    )
    order = {image.id: i for i, image in enumerate(images)}
    for change in result.changes:
        if (
            not set(change.frame_ids) <= order.keys()
            or len(set(change.frame_ids)) != len(change.frame_ids)
            or change.frame_ids != sorted(change.frame_ids, key=lambda fid: order.get(fid, -1))
        ):
            raise ProviderError("视觉观察引用不属于当前片段、重复或时间反向的图片，未采用")
    return result


async def audit_answer(vlm, scenario, focus, answer, images, reviews):
    result = await vlm.structured(
        "审查一段准备发给用户的回答是否超出视觉证据，只检查，不增加新的任务要求。"
        "这是弱视觉证据审查，不是动作或安全认证；观察报告也可能错误，须核对原图。"
        "以下说法若仅依据稀疏图片就是不支持的：连续节奏缓慢/轻柔、精确次数、发力/痛感、"
        "不存在某动作、整体正确/错误、内部连接牢固、设备可以安全通电。"
        "只报告可见变化；‘看不清’不得推导为‘做错’，不能因此让用户重做。"
        "检查回答是否仍针对讨论目标，而非误用当前界面步骤。检查是否编造了遮挡或机位问题。"
        "保留有依据的部分，不因不能计数就否定可见变化。不要重复既有问题或增加检查问卷。"
        "若原回答有上述问题，action=revise，corrected_reply 给出2至3句直接、自然的修正版，corrected_headline 给出不超过30字的一句话结论，"
        "不加入新动作建议，不把未知写成事实；evidence_ids 只引用本次提供的原图。否则keep。"
        + skill(scenario)["guidance"]
        + "\n"
        + dump({"focus": focus, "draft": answer.model_dump(), "observations_not_facts": reviews}),
        AnswerAudit,
        images,
    )
    if not set(result.evidence_ids) <= {im.id for im in images}:
        raise ProviderError("回答审查引用未提供的图片，未采用")
    return result
