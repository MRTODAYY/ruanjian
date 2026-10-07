# Code Review Agent · 代码审查智能体

> **软件工程 Homework 1 —— Code Agent**
>
> 一个可运行、可解释、可扩展的代码审查 Agent。它不只是「把代码丢给大模型」，
> 而是让模型**通过工具去求证**：静态分析算出客观度量，运行期工具真实执行代码复现 Bug，
> 最后才由模型给出带行号和证据的审查报告。

---

## 一、这个项目解决了什么问题

直接用 ChatGPT / DeepSeek 审查代码，常见的三个痛点是：

| 痛点 | 本项目的解法 |
|------|--------------|
| **编造行号**：「第 42 行有个空指针」——可第 42 行是注释 | 行号由本地 AST 分析工具产出，模型只能引用工具给出的事实 |
| **不敢下结论**：「建议检查一下除零风险」——到底会不会崩？ | `execute_code` / `analyze_with_runtime` 真实运行代码，用实测结果说话 |
| **漏掉真缺陷**：凭感觉扫一遍，复杂逻辑里的问题看不见 | 26 条静态规则 + 11 类坏味道 + 边界输入探测，覆盖人工容易忽略的模式 |

**一句话总结设计哲学：让工具负责「事实」，让模型负责「判断」。**

---

## 二、快速开始

### 2.0 最省事的方式：双击 `.bat` 文件（推荐给不熟悉命令行的同学）

项目根目录已经准备好 4 个启动器，**双击就能用，不需要敲任何命令**：

| 双击这个文件 | 会发生什么 | 花不花钱 |
|--------------|------------|----------|
| **`1-打开网页版.bat`** | 自动打开浏览器，出现图形界面。在网页上选文件、点按钮就行 | 花钱（用真实 API） |
| **`2-演示模式-不花钱.bat`** | 黑窗口里跑一遍完整流程，用假的 AI 回复演示 | **不花钱** |
| **`3-审查示例代码.bat`** | 审查自带的示例代码，结果存到 `reports/` 文件夹 | 花钱（约 1-2 分钱） |
| **`4-审查我自己的代码.bat`** | 提示你粘贴文件路径，然后审查你的代码 | 花钱 |

> **第一次用建议按 2 → 1 的顺序**：先双击 `2` 看不花钱的演示，心里有数了，
> 再双击 `1` 打开网页版正式用。

> ⚠️ `.bat` 窗口里的**黑色窗口不要关闭**（网页版需要它当后台服务），
> 用完了直接关掉窗口即可。

### 2.1 环境要求

* Python **3.8+**（开发验证于 3.12）
* **零第三方依赖** —— 只用标准库，不需要 `pip install` 任何东西

### 2.2 配置 API Key

**如果你用双击 `.bat` 的方式，这一步已经帮你做好了**（`.env` 文件已配置好）。

如果你要自己配置（换 Key、换模型）：

```bash
# 复制配置模板
cp .env.example .env        # Windows: copy .env.example .env
```

编辑 `.env`，填入你的密钥（支持任何 OpenAI 兼容服务）：

```ini
# DeepSeek（默认，性价比高）
CODEAGENT_BASE_URL=https://api.deepseek.com
CODEAGENT_API_KEY=sk-你的密钥
CODEAGENT_MODEL=deepseek-chat

# 也可以换成 OpenAI / 通义千问 / Kimi / 本地 Ollama：
# CODEAGENT_BASE_URL=https://api.openai.com/v1
# CODEAGENT_MODEL=gpt-4o-mini
```

> 也可以不用 `.env`，直接设置环境变量 `CODEAGENT_API_KEY`，或用 `--api-key` 参数临时指定。

### 2.3 不需要 Key 也能先跑通

```bash
python -m agent selftest      # 16 项离线自检（工具、分析器、Agent 循环、容错）
python -m agent review demo/sample_buggy.py --mock   # 用假 LLM 走一遍完整流程
```

### 2.4 开始审查

```bash
python -m agent review demo/sample_buggy.py          # 审查单个文件
python -m agent review src/ --report                 # 审查整个目录并存报告
```

---

## 三、功能一览

### 3.1 五种工作模式

| 命令 | 模式 | 用途 |
|------|------|------|
| `python -m agent review <目标>` | 代码审查 | 找缺陷、评级、给修复代码（**主功能**） |
| `python -m agent explain <目标>` | 代码解释 | 逐段讲清逻辑、隐藏假设与陷阱 |
| `python -m agent test <目标>` | 测试生成 | 基于边界探测结果生成 `unittest` 用例 |
| `python -m agent refactor <目标>` | 重构建议 | 坏味道清单 + 重构后代码 + 等价性验证 |
| `python -m agent ask "<问题>"` | 自由问答 | 就代码库任意提问 |

其他命令：

```bash
python -m agent repl        # 交互式多轮对话（带记忆，支持 :mode 切换）
python -m agent tools       # 查看工具清单与参数
python -m agent version     # 查看版本与当前配置
python -m agent.web         # 启动 Web 界面
```

> ⚠️ **参数顺序**：`--model` / `--mock` / `--verbose` / `--no-color` 等是
> **全局参数，必须写在子命令之前**（这是 Python `argparse` 的标准行为）：
>
> ```bash
> python -m agent --no-color tools              # ✅ 正确
> python -m agent --mock review demo/x.py       # ✅ 正确
> python -m agent review --mock demo/x.py       # ❌ 报错：unrecognized arguments
> ```

### 3.1.1 常用参数

| 参数 | 说明 |
|------|------|
| `--report` | 把报告保存到 `reports/` 目录（含执行轨迹） |
| `--json FILE` | 把结构化结果（含完整 trace）保存为 JSON |
| `--goals "…"` | 追加审查重点，如 `--goals "重点看并发安全"` |
| `--mock` | 离线 Mock 模式，用内置假回复演示全流程，不消耗 API 额度 |
| `--no-exec` | 禁用代码执行类工具（只做静态分析） |
| `--max-steps N` | 覆盖 Agent 最大步数 |
| `-q` / `-v` | 只输出报告 / 打印详细日志 |

### 3.2 实际效果

下面是 `python -m agent review demo/sample_buggy.py` 的真实运行片段
（`demo/sample_buggy.py` 是故意写坏的示例，内含 10 处典型缺陷）：

```
  [1/14] 推理中…
     │ I'll start by exploring the project structure and then dive into the target file.
     ├─ 调用工具 list_files {"path": "demo", "pattern": "*"}
     │  ✔ 返回（1 ms）

  [2/14] 推理中…
     │ Now let me read the target file and run static analysis in parallel.
     ├─ 调用工具 read_file {"path": "demo/sample_buggy.py"}
     ├─ 调用工具 static_analyze {"path": "demo/sample_buggy.py"}
     ├─ 调用工具 detect_code_smells {"path": "demo/sample_buggy.py"}
     │  ✔ 返回（9 ms）

  [3/14] 推理中…
     │ Now let me verify the runtime behaviors — the bugs I suspect.
     ├─ 调用工具 analyze_with_runtime {"path": "demo/sample_buggy.py"}
     ├─ 调用工具 execute_code {"code": "import importlib.util ..."}
     │  ✔ 返回（83 ms）
```

最终报告节选（**每条结论都经过真实执行验证**）：

> ### 🔴 严重
>
> 1. **`demo/sample_buggy.py:48` `eval` 执行任意代码（代码注入）**
>    - 证据：`return eval(expr)`
>    - 影响：**已验证** `parse_expression("__import__('os').getcwd()")` 成功返回当前工作目录。
>    - 修复：改用 `ast.literal_eval` 并捕获 `ValueError/SyntaxError`。
>
> 2. **`demo/sample_buggy.py:118` 空值守卫被前置语句绕过（死代码）**
>    - 现象：第 121 行的 `if order is None: return None` 是**永远无法生效**的守卫
>      ——第 118 行 `tmp = order.get("debug")` 先执行，`order` 为 `None` 时直接崩溃。
>    - 影响：**已验证** `process_order(None, reg)` 抛 `AttributeError`。
>
> 3. **`demo/sample_buggy.py:59` 与 `:67` 除零崩溃**
>    - 影响：**已验证** `average_order_amount([])` 抛 `ZeroDivisionError`。

值得注意的两点：

* 第 2、3 条中的**守卫顺序问题**和**优惠券负金额问题**并不在示例文件自述的缺陷清单里，
  是 Agent 通过「运行期探测 + 交叉比对」额外发现的。
* 报告里包含**实测性能数据**：`format_report` 的循环字符串拼接比 `"".join()`
  慢约 **117 倍**（2 万行数据：0.5841s vs 0.005s）——这是 `execute_code` 真跑出来的数字。

### 3.3 Web 界面

```bash
python -m agent.web --port 8000 --open
```

零依赖实现（只用标准库 `http.server` + SSE 实时推送），页面左侧配置任务、
右侧**实时滚动显示 Agent 的推理与工具调用**，最后渲染 Markdown 报告：

* 顶部显示当前模型、Key 状态、工作目录、可用工具数
* 可点击的文件列表直接填入目标路径
* 工具调用实时可见（名称、参数、耗时、返回预览）
* 报告在前端做轻量 Markdown 渲染并统计步数 / 工具次数 / Token

---

## 四、Agent 是怎么工作的

### 4.1 核心循环

```
                    ┌──────────────────────────────────────┐
   用户任务  ──────▶ │  1. 组装上下文（系统提示 + 工具 Schema）│
                    └──────────────────┬───────────────────┘
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  2. LLM 推理：我要不要调工具？调哪个？  │
                    └──────────────────┬───────────────────┘
                          ┌────────────┴────────────┐
                    无工具调用                  有工具调用
                          ▼                         ▼
              ┌───────────────────┐   ┌──────────────────────────────┐
              │ 3. 输出最终审查报告 │   │ 4. 工具真实执行（本地，非模拟） │
              └───────────────────┘   └──────────────┬───────────────┘
                                                     ▼
                                      ┌──────────────────────────────┐
                                      │ 5. Observation 回填进对话历史 │
                                      └──────────────┬───────────────┘
                                                     └──────▶ 回到第 2 步
```

这个「推理 → 行动 → 观察」的闭环，就是 Agent 与「单次问答」的本质区别。

### 4.2 八个工具

| 工具 | 类型 | 作用 |
|------|------|------|
| `list_files` | 只读 | 列目录，先摸清项目结构 |
| `read_file` | 只读 | 读文件（带行号，支持分段） |
| `static_analyze` | 分析 | **AST 静态分析**：度量 + 26 条规则的问题清单 |
| `detect_code_smells` | 分析 | 坏味道检测（重复代码、深层嵌套…） |
| `write_report` | 输出 | 把结论落盘成 Markdown 报告 |
| `execute_code` | ⚠️ 执行 | 受限子进程真实运行 Python 代码 |
| `run_tests` | ⚠️ 执行 | 跑 pytest / unittest 验证正确性 |
| `analyze_with_runtime` | ⚠️ 执行 | 用边界输入探测真实运行期异常 |

### 4.3 静态分析能查出什么

以 `demo/sample_buggy.py`（220 行 / 14 个函数）为例，静态分析报出
**1 严重 + 9 重要 + 6 次要 + 13 提示**，覆盖规则包括：

**正确性**：除零风险、可变默认参数、可变类属性、死代码、除零守卫识别
**异常处理**：裸 `except`、静默吞异常、`finally` 中改变控制流
**安全**：`eval`/`exec`、`pickle.loads`、`shell=True` 命令注入、`yaml.load`
**资源**：文件句柄未释放
**可维护性**：圈复杂度、嵌套深度、函数长度、参数过多、重复代码、未使用变量/导入
**规范**：类型注解覆盖率、文档覆盖率、`== None` 比较、魔法数字、遗留 TODO

对比验证——对写得很规范的 `demo/sample_clean.py`（93 行）：
**0 严重、0 重要**，只有 1 条次要 + 6 条提示。
这个对比证明了**误报抑制**是有效的：好的代码不会被噪声淹没。

### 4.4 项目结构

```
code-review-agent/
├── agent/
│   ├── cli.py           # 命令行入口（review/explain/test/refactor/ask/repl/tools/selftest）
│   ├── web.py           # 零依赖 Web 界面（http.server + SSE）
│   ├── core.py          # ★ Agent 主循环：推理→工具调用→结果回填
│   ├── prompts.py       # ★ Prompt 模板集中管理（5 种模式）
│   ├── llm.py           # ★ LLM 客户端：OpenAI 兼容 + 重试 + Mock
│   ├── analyzer.py      # ★ AST 静态分析引擎（26 条规则）
│   ├── config.py        # 配置加载（.env + 环境变量，自己解析，零依赖）
│   ├── console.py       # 终端输出（UTF-8 / ANSI 颜色）
│   └── tools/
│       ├── base.py          # 工具注册表 + JSON Schema 校验
│       ├── common.py        # 路径沙箱、文件读取
│       ├── local_tools.py   # 只读 + 分析类工具
│       ├── exec_tools.py    # 执行类工具（含运行期探测）
│       └── report_tool.py   # 报告落盘
├── demo/
│   ├── sample_buggy.py          # 故意写坏的示例（10 处缺陷）
│   ├── sample_clean.py          # 规范代码示例（对比用）
│   └── generated_tests/         # Agent「测试生成」模式的产物示例
├── tests/               # 154 个单元测试，全部离线可跑
├── reports/             # Agent 生成的审查报告
├── .env.example         # 配置模板
└── README.md / Design.md
```

代码规模：**24 个 Python 文件、6610 行（代码 4963 行）、409 个函数 / 67 个类**。
其中 `agent/` 主体 16 文件 4539 行，`tests/` 5 文件 1321 行，`demo/` 3 文件 750 行。

---

## 五、测试与自检

```bash
python -m unittest discover -s tests -t . -v    # 154 个单元测试
python -m agent selftest                        # 16 项端到端自检
```

测试覆盖的**关键行为**（不只是「跑通」）：

| 类别 | 验证内容 |
|------|----------|
| Agent 循环 | 多步闭环、并行工具调用、`max_steps` 收敛、轨迹完整 |
| **容错** | 工具异常 / 未知工具 / LLM 故障 / 回调异常全部不中断流程 |
| **重试** | 429/5xx/网络超时重试，401/404 **不重试**（避免浪费额度） |
| **安全** | 路径穿越拦截、禁用执行时工具不可见、文件目录越界拦截 |
| **消息协议** | 上下文压缩后 `tool` 消息不得成为孤儿（真实 API 曾因此返回 HTTP 400） |
| 上下文 | 长历史自动压缩、多轮记忆、长期记忆注入 |
| 分析器 | 26 条规则逐条断言、**误报抑制**（优质代码 0 严重问题） |
| 信号量 | 中文内容 HTTP 往返不乱码、JSON 可序列化 |

所有 LLM 测试都通过替换 `urllib.request.urlopen` 完成，**不产生真实网络请求**。

> 但离线测试并不能覆盖一切：项目的第 5 个真实缺陷（上下文压缩破坏
> `tool_calls` 配对导致 HTTP 400）就是**只有真实 API 才能暴露**的。
> 因此本项目同时保留了两条验证路径 —— 154 个离线测试保证逻辑正确，
> 真实 API 端到端运行保证集成正确。详见 [Design.md](Design.md#114-开发过程中被抓到的真实缺陷)。

---

## 六、安全说明

本项目内置了多层防护，但也必须说清它的边界：

**已实现的防护**

1. **开关**：`CODEAGENT_ENABLE_EXEC=false` 时执行类工具**不注册**，
   模型在工具列表里看不到它们，从根上杜绝调用。
2. **超时**：所有子进程强制超时（默认 20s），防止死循环卡死 Agent。
3. **路径沙箱**：所有文件访问被限制在 `CODEAGENT_WORKSPACE` 内，
   `../` 穿越与「同前缀兄弟目录」（`/tmp/ws-evil` vs `/tmp/ws`）都会被拒绝。
4. **输出截断**：工具输出与文件读取都有长度上限，防止撑爆上下文。
5. **密钥脱敏**：日志与 `version` 输出中 Key 始终显示为 `sk-abc***wxyz`。
6. **`.env` 已加入 `.gitignore`**，不会被误提交。

**⚠️ 必须知道的边界**

* 这是**作业级沙箱，不是生产级隔离**。`execute_code` 执行的是**真实代码**，
  只靠超时和路径限制，**不能防御恶意代码**。生产环境请放进容器 / 虚拟机 / gVisor。
* 模型可能被诱导去执行危险操作 —— 所以真正的防线是第 1 条：**不用的能力就不要注册**。
* API Key 明文存在 `.env` 中。不要在公共机器上这样做，也不要把 Key 提交到仓库。

---

## 七、常见问题

**Q：报错「未找到可用的 API Key」？**
按提示任选一种方式配置，或先加 `--mock` 体验完整流程。

**Q：报错 HTTP 404？**
`CODEAGENT_BASE_URL` 或 `CODEAGENT_MODEL` 不对。程序会自动适配
`.../v1` 与 `.../v1/chat/completions` 两种写法，你只要填服务商根地址即可。

**Q：Agent 停在「已达到最大步数」？**
复杂项目可调大 `.env` 里的 `CODEAGENT_MAX_STEPS`（默认 14）。
注意程序不会因此崩溃 —— 它会输出已收集到的分析素材。

**Q：怎么换成别的模型？**
改 `.env` 里的 `CODEAGENT_BASE_URL` 和 `CODEAGENT_MODEL` 即可，
或临时用 `--base-url` / `--model` 覆盖。任何 OpenAI 兼容接口都能用。

**Q：终端中文乱码？**
程序已自动把 stdout 切到 UTF-8。若仍乱码，执行 `chcp 65001`（Windows）。

**Q：审查时会不会把我的代码传给第三方？**
会。审查需要把代码内容发送给所配置的 LLM 服务商。涉密代码请改用本地模型
（如 Ollama：`CODEAGENT_BASE_URL=http://localhost:11434/v1`）。

---

## 八、交付物对照

按照作业要求的提交清单：

| 要求 | 本项目对应内容 |
|------|----------------|
| 提交物1／代码 | `agent/`（16 文件 4481 行）+ `tests/` + `demo/`，纯标准库可直接运行 |
| 提交物2／文档 | 本 `README.md`（使用说明）+ `Design.md`（架构与设计决策） |
| 技术要求1／Agent 循环 | `agent/core.py`：输入→推理→工具调用→输出，带轨迹可审计 |
| 技术要求1／至少一种工具 | 实际实现 **8 个工具**（文件、静态分析、代码执行、测试、报告） |
| 技术要求1／命令行或 Web | **两者都有**：完整 CLI + 零依赖 Web 界面 |
| 技术要求2／主流框架或原生 API | 原生 **OpenAI 兼容 API**（`urllib` 直连，零依赖，服务商无关） |
| 技术要求2／上下文记忆 | 多轮对话历史 + 长期记忆 + 超长自动压缩（`core.py`） |
| 技术要求2／错误处理与重试 | 指数退避重试、错误分类（可重试/不可重试）、全链路容错 |
| 评审／功能完整性 40% | 5 种模式 + 8 个工具，含边界情况处理 |
| 评审／Agent 架构 30% | 分层清晰：LLM / Prompt / 工具 / 循环 / CLI / Web 解耦 |
| 评审／代码质量 20% | 全中文注释与 docstring、类型注解、154 个测试 |
| 评审／文档 10% | README + Design.md（含设计权衡与备选方案对比） |

核心设计决策与权衡详见 **[Design.md](Design.md)**。
