# 微缩 issue 回放评测集（eval/）

把"我演示过一次成功"升级成"我有带标准答案的固定题库，每次改动重考，用成功率说话"。
对应 notes/enterprise-ai-coding.md 的缺口 B，思想是缩小版 SWE-bench：真实小项目 + 隐藏判分测试。

## 为什么需要它

真模型输出有随机性，同任务跑两次结果可能不同。单次联调（n=1）只能证明"能跑通"，
回答不了"改了一版压缩策略/换了模型，到底变好还是变差"。本目录提供：
- 固定题库（kata），题和判分标准不变；
- 每次试验在独立干净副本里跑，setup 原件永不被改，可无限重复；
- judge 只看世界终态（测试退出码），不看 Agent 自述；
- 同题重复 N 次统计 pass@1，并把结果带时间戳落盘，供跨版本对比。

## 目录约定

```
eval/
├── runner.py          离线引擎：discover / fresh_copy / run_once / run_repeated / summarize
├── test_runner.py     runner 自身的机制测试（假 agent，零 token，可进回归）
├── run_live_eval.py   真模型适配层：把 mini_agent.run_agent 包成 agent_fn（烧 token）
├── katas/
│   └── todo_mark_done/
│       ├── issue.md    自然语言任务（给 Agent 看的题面）
│       ├── setup/      有缺陷的项目起点（被复制，永不改动）
│       │   └── todo.py
│       └── judge.py    客观判分脚本，退出码 0=修好，非 0=没修好
├── results/           live 结果 JSON（带时间戳，git 可忽略）
└── .eval_work/        每次试验的临时副本和 trace（git 可忽略）
```

## 一个 kata 的自检纪律（加新题必做）

1. 缺陷起点直接跑 judge 必须是红（exit 非 0）——证明测试真能抓住这个 bug；
2. 手工正确修复后再跑 judge 必须转绿——证明答案存在、判分标准本身正确。
只绿不红的判分等于没有判分。

## 怎么跑

离线机制测试（不花钱，日常回归）：
```
cd mini-agent/eval && ../.venv/bin/python test_runner.py
```

真模型回放（需要 loop/.env 的 DASHSCOPE_API_KEY，真实计费）：
```
../.venv/bin/python run_live_eval.py --times 5                 # 全部 kata 各 5 次
../.venv/bin/python run_live_eval.py --times 1 --kata todo_mark_done
```

## 判分映射到 SWE-bench 概念

- judge 里"修复前失败、修复后必须过"的断言 = FAIL_TO_PASS（证明真修好）；
- "add/list 既有行为不得被改坏"的断言 = PASS_TO_PASS（回归保护）；
- 重复 N 次的通过比例 = pass@1（日常一次成功率，不是 pass@k 取最好的障眼法）。

## 诚实边界

- runner 机制由 20 条离线断言保证，但 Agent 能力结论只在真模型、足够样本下才成立；
- 目前只有 1 个 kata，题目数量远不足以谈稳定成功率，目标是先把"可量化回归"的管道打通；
- judge 覆盖的是被断言的行为，绿了不等于语义完美，仍存在测试不全的问题；
- 真模型单次结果会波动，对比两个策略时要跑相同 kata、相同次数，差异才归因得到。

## 加新 kata 的步骤

1. 建 katas/<名字>/，放 issue.md、setup/（缺陷项目）、judge.py；
2. 按上面的自检纪律验证红→绿；
3. 无需改 runner——discover 会自动扫到；
4. 离线 test_runner.py 和真模型 run_live_eval.py 都能直接带上它。
