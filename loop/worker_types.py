"""
worker_types.py — 子 Agent 类型的声明式注册表（2026-11-02，第七课）

对齐 Claude Code 的 AgentTool 设计：每种子 Agent 是一份【声明】，不是一段
if/else 派工逻辑。主模型通过工具描述里的 when_to_use（招聘 JD）自己选类型，
运行时按类型名查本表，取出该类型的：系统提示、工具白名单（硬收窄）、预算。

  - when_to_use 是「软引导」：拼进工具描述给模型看，影响它选不选这个类型；
  - tool_names 是「硬约束」：运行时真的只给这些工具，模型选错类型也越不了权。

新增一种工人：在下面 WORKER_TYPES 加一份声明即可，主循环和 dispatch 逻辑
一行都不用改（开闭原则），它会自动出现在工具描述里、可被模型选用。
"""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class WorkerType:
    name: str
    when_to_use: str          # 给主模型看的「什么时候派我」，一句话
    tool_names: List[str]     # 硬约束：该类型实际可用的工具名
    system_prompt: str        # 该类型工人的独立人设/职责
    max_turns: int = 4


# 只读勘察员：快、便宜、只找代码不改动。对应 Claude Code 的 Explore。
EXPLORE = WorkerType(
    name="explore",
    when_to_use=(
        "快速在代码/文件里定位实现、按关键词或路径找东西、回答「这块是怎么做的」。"
        "只读，不改动任何文件，适合调研后把结论交回主脑。"
    ),
    tool_names=["read_file", "search_in_file"],
    max_turns=3,
    system_prompt=(
        "你是一名只读代码勘察员，擅长快速定位文件和实现。\n"
        "你只能读取/检索，严禁声称或尝试修改、创建、删除文件。\n"
        "尽快给出结论：相关文件路径、关键行、简明事实，不要贴大段原文，不要给改造方案。"
    ),
)

# 只读规划员：看得更细，产出分步实现方案+关键文件，但绝不动手。对应 Plan。
PLANNER = WorkerType(
    name="planner",
    when_to_use=(
        "需要在动手前设计实现方案、权衡取舍、列出分步计划和关键文件时使用。"
        "只读、只出计划，不写代码、不执行修改。"
    ),
    tool_names=["read_file", "search_in_file"],
    max_turns=5,
    system_prompt=(
        "你是一名软件架构与实现规划专家，只做只读调研和方案设计，严禁修改任何文件。\n"
        "你的产出必须包含：分步实现计划、涉及的关键文件路径、风险与取舍。\n"
        "结尾用「关键文件：」列出最多 5 个最关键的文件。你不负责实现。"
    ),
)

# 沙箱操作员：唯一带 run_shell 的类型，跑命令仍走主循环同一权限闸 + seatbelt。
# 用来演示「工具白名单是硬约束」：explore/planner 连这个工具都看不到。
OPS = WorkerType(
    name="ops",
    when_to_use=(
        "需要实际执行命令来获取信息时使用（如 ls、git status、git log、运行构建/测试）。"
        "命令仍需经权限审批并在 OS 沙箱内运行；不要用它做与任务无关的操作。"
    ),
    tool_names=["read_file", "search_in_file", "run_shell"],
    max_turns=4,
    system_prompt=(
        "你是一名沙箱内的命令操作员。需要时用 run_shell 获取信息，"
        "命令在受限沙箱中运行且需审批；只读优先，避免任何破坏性操作，"
        "报告命令观察到的事实即可。"
    ),
)


# 注册表：名字 -> 声明。新增工人类型只改这里。
WORKER_TYPES = {
    EXPLORE.name: EXPLORE,
    PLANNER.name: PLANNER,
    OPS.name: OPS,
}

DEFAULT_WORKER_TYPE = EXPLORE.name


def get_worker_type(name: str):
    """按名字取声明；未知类型返回 None（调用方应拒绝，而不是兜底放权）。"""
    return WORKER_TYPES.get(name)


def render_worker_catalog() -> str:
    """把注册表渲染成工具描述里的「可选工人清单」。

    对应 Claude Code 的 formatAgentLine：
      `- name: when_to_use (Tools: a, b)`
    """
    lines = ["可选工人类型（通过 worker_type 指定，缺省为 explore）："]
    for wt in WORKER_TYPES.values():
        lines.append(f"- {wt.name}: {wt.when_to_use} (Tools: {', '.join(wt.tool_names)})")
    return "\n".join(lines)
