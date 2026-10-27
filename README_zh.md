# Clinic Phone Agent — 小诊所 AI 电话前台（骨架）

[English](README.md) | **中文**

接电话 → 回答诊所常见问题 → 预约 / 改约 / 取消 → 处理不了的留言或转真人 → 通话结束后写摘要给前台。

> 这是一个**能跑、带测试的骨架**，没做优化。先读第 1–3 节理解"这事行业里怎么做"，再看第 4 节怎么跑。

---

## 1. 行业里一般怎么做（三条路）

这个问题定义得很清楚，行业里基本就三种做法：

| 路线 | 代表 | 你要做的事 | 适合 |
|---|---|---|---|
| **A. 直接买成品** | 各种 "AI receptionist for dental/medical" 的垂直 SaaS | 配置 + 对接 | 诊所自己用，不想要技术团队 |
| **B. 托管语音 Agent 平台** | Vapi、Retell AI、Bland、Synthflow | 写 prompt、把"查空档 / 预约"做成 webhook 给平台调用 | 代理公司 / 外包最常见，上线最快 |
| **C. 自己搭** | Twilio（电话）+ OpenAI Realtime（语音模型），或者 Pipecat / LiveKit Agents 框架 | 自己负责音频桥接、工具调用、日志 | 想掌控数据、成本、合规，或者要做成产品 |

不管选哪条，**架构其实都一样**，只是哪层是你写、哪层是买：

```
电话网络 (PSTN)
   │
电话运营商 / 号码 (Twilio)          ← B 路线里平台帮你包了
   │  音频流
语音模型 (speech-to-speech：OpenAI Realtime；
         或者三段式：STT → LLM → TTS)        ← B 路线里平台帮你包了
   │  工具调用 (function calling)
你的业务后端：排班规则、患者、预约、留言   ← 不管哪条路线，这层都得你写
   │
诊所现有系统（EHR / 预约软件）或者我们自己的 DB
```

**这个仓库走的是 C 路线**，理由如下：

- 你已经用过 OpenAI Realtime，最熟。
- 面试要的是"能不能做"，自己搭能证明你理解每一层。之后真要快速交付，换成 B（Vapi/Retell）也只是把"语音那层"换掉，**`app/scheduling.py` + `app/tools.py` 这层原封不动**——它们本来就是 webhook 能直接调的普通函数。
- 体量小：一两百个病人，一天大概十几到几十通电话。**一台小服务器 + SQLite/Postgres 就够了**，不需要队列、K8s、微服务。

### 关于"一两百个客户"——面试时一定要问清楚

- 如果是**一家诊所有一两百个病人** → 就是这个仓库现在的单租户形态。
- 如果是**这家公司服务一两百家诊所** → 要做多租户：`clinic.json` 改成数据库里的 `clinics` 表，按被叫号码（Twilio 的 `To` 参数）找到对应的诊所配置/prompt/数据。代码改动不大，但架构讨论的重点就不一样了。

---

## 2. 关于 RAG：这个体量**不需要**

小诊所的"知识"就是地址、停车、营业时间、取消政策、保险……几页纸，几千字。
**直接全塞进 system prompt** 比 RAG 更好：没有检索延迟（电话里每 300ms 都能听出来）、不会漏检、prompt cache 友好。

`app/knowledge.py` 已经做了自动切换：知识库 < 12k 字符时内联进 prompt；超过以后自动给模型加一个 `search_clinic_info` 工具（用的是很小的 BM25，零依赖）。真到几百份文档再换 embedding + pgvector。

**真正需要"检索"的是结构化数据**——空档、预约、患者——这些靠**工具调用查数据库**，不是 RAG。

---

## 3. 架构和关键设计（面试时讲这些）

```
来电 ──> Twilio 号码 ──POST /twilio/voice──> 返回 TwiML <Connect><Stream>
                │
                └── wss /twilio/media-stream  (G.711 μ-law 8kHz)
                          │   app/telephony/twilio_stream.py
                          │   (音频桥 + 打断处理)
                          │
                          └── wss OpenAI Realtime  (app/realtime_agent.py：
                                     │   会话配置、工具循环、转写)
                                     │ function call
                                     v
                            app/tools.py  (工具定义 + 分发，身份绑定在通话上下文)
                                     │
                            app/scheduling.py  (纯业务规则，不碰 LLM)
                                     │
                            app/db.py  (SQLite → Postgres)
通话结束 ──> app/call_lifecycle.py：存转写 + 工具调用记录 + LLM 摘要 → /admin/calls 给前台看
```

### 设计原则

1. **模型只"提议"，代码来"执行"。** 营业时间、提前量、防重复预约、医生和服务是否匹配，全都在 `scheduling.py` 里硬校验。模型说错时间也订不进去。写入时会再查一次冲突（slot 可能刚被别人占了）。
2. **身份绑定在通话上，不在参数里。** `verify_patient`（姓名 + 生日）成功后，patient_id 存在 `CallContext` 里；工具参数里**根本没有 patient_id**，所以来电者再怎么"说服"模型，也动不了别人的预约。
3. **永远有退路。** 处理不了 → `take_message` 留言；客户要求 / 情绪不好 / 卡住两次 → `transfer_to_human` 转前台；急症 → 让对方挂了打 911（写在 prompt 安全规则最前面）。
4. **不给医疗建议，不报检查结果。**
5. **说完再挂。** `end_call` / 转接不会立刻执行：等模型把"再见"说完，Twilio 回传 `mark` 事件确认音频播完了，才真正挂断或转接。
6. **打断（barge-in）。** 来电者一开口：清掉 Twilio 里还没播的音频 + `conversation.item.truncate` 告诉模型"你只说到这里"，模型就不会以为对方听到了没播出来的部分。
7. **文字和语音用同一套工具。** `scripts/text_chat.py` 用同样的 prompt、工具和数据库，打字就能测完整业务流程，不用每次都打电话。

### 两种电话接入方式（都已实现）

- **默认：Twilio Media Streams**（`twilio_stream.py`）——音频经过我们的服务器。可控、可调试，资料最多。
- **可选：OpenAI SIP**（`openai_sip.py`）——Twilio Elastic SIP Trunk 直接连到 `sip:<project_id>@sip.api.openai.com`，OpenAI 发 `realtime.call.incoming` webhook，我们 accept 后挂一个 sideband websocket 只处理工具调用。少一跳，延迟更低，但跟 OpenAI 绑得更紧。

---

## 4. 怎么跑

```bash
uv sync
```

```bash
cp .env.example .env
```

填好 `OPENAI_API_KEY`（Twilio 那几项先不填也行）。

```bash
uv run python -m scripts.seed
```

**不用电话，先打字测：**

```bash
uv run python -m scripts.text_chat
```

可以试："我是 Alice Nguyen，生日 1985-04-12，想把明天的预约改到下周三下午。"

**跑测试：**

```bash
uv run pytest -q
```

**接真电话：**

1. 启动服务：
   ```bash
   uv run uvicorn app.main:app --port 8000
   ```
2. 暴露公网 https，然后把地址填进 `.env` 的 `PUBLIC_BASE_URL`：
   ```bash
   ngrok http 8000
   ```
3. Twilio 控制台 → 买一个号码 → Voice "A call comes in" → Webhook `POST https://<你的域名>/twilio/voice`。
4. 打这个号码。

**前台接口**（`Authorization: Bearer <ADMIN_TOKEN>`）：`GET /admin/appointments?day=YYYY-MM-DD`、`/admin/messages`、`/admin/calls`。

---

## 5. 代码地图

| 文件 | 作用 |
|---|---|
| `clinic.json` | 诊所配置：时区、营业时间、休息日、服务项目和时长、医生、转接号码 |
| `knowledge/*.md` | FAQ / 政策（诊所直接改 markdown 就行） |
| `app/scheduling.py` | 排班规则：找空档、预约、改约、取消、患者查找/注册 |
| `app/tools.py` | 给模型的 10 个工具 + 分发器 + `CallContext` |
| `app/prompts.py` | system prompt（角色、语气、安全规则、流程，每通电话注入当前时间和来电号码） |
| `app/realtime_agent.py` | 跟传输方式无关的 Realtime 会话处理：配置、工具循环、转写 |
| `app/telephony/twilio_stream.py` | Twilio ⇄ OpenAI 音频桥、打断、说完再挂、签名校验 |
| `app/telephony/openai_sip.py` | SIP 直连方式 |
| `app/call_lifecycle.py` | 通话记录落库 + 结束后 LLM 摘要 |
| `app/admin.py` | 前台查看接口 |
| `scripts/text_chat.py` | 打字测试 |
| `tests/` | 排班规则、工具权限、TwiML，还有一个用假 OpenAI 服务的端到端桥接测试 |

---

## 6. 模拟电话实测（2026-10-17，真的 OpenAI Realtime）

`scripts/simulate_call.py`：用"假来电者"（LLM 扮演角色 + TTS 合成语音）代替 Twilio，以真实电话的格式（8kHz μ-law、实时推流、回传播放标记）打到 `/twilio/media-stream`。服务器那边走的是生产代码。每通电话在 `data/sim_calls/<场景>/` 下输出 `transcript.md` 和双声道录音 `call.wav`。

```bash
uv run python -m scripts.simulate_call
```

一共跑了 4 轮，前后找到并修掉的问题：

| 问题 | 原因 | 修法 |
|---|---|---|
| 来电者说完以后要等 4–8 秒 agent 才开口 | `semantic_vad` 在电话音质下判断"说完了"太慢 | 改用 `server_vad` 600ms，延迟稳定在 1.1–2 秒；可通过 `VAD_TYPE` 配置 |
| 诊所下班了还"转真人"（转过去会没人接） | 转接前没检查营业时间 | `transfer_to_human` 在代码里检查营业时间，关门时让 agent 改为留言 |
| 新患者被订给了不收新病人的 Chen 医生 | 这条规则只写在 FAQ 里 | `clinic.json` 增加 `accepting_new_patients`，查空档和预约时由代码强制 |
| 续药留言没有生日，前台找不到病历 | 只靠 prompt 约束，模型时灵时不灵 | `take_message` 增加 `category`；续药、账单、检查结果三类没给生日就拒绝保存 |
| 中文姓名：丽 / 李 / 力 分不清，登记成了"李王" | 电话里同音字没法区分 | 改问健康卡上的英文名，逐个字母复述 |
| 中文场景把 10 月说成 11 月 | 模型翻译英文月份时口误 | 工具同时返回数字日期 `date` |
| 问候语没有诊所名字；一个回合里说三遍"稍等"；念出 prompt 规则 | prompt 写法 | 改 prompt |
| 订完直接挂线，没问"还有别的吗" | prompt 写法 | 改 prompt：先问还有没有事，说完再见立刻调用 `end_call` |
| 先说"正在为您转接"，查完又改口"诊所关门了" | prompt 写法 | 工具成功之后才宣布转接 |

### 多语言（2026-10-18）

示例诊所的医生是编的：沈伟医生（英语、普通话、粤语）、Patel 医生（只会英语，不收新病人）、黄护士（英语、粤语），前台会英语和普通话。换成真实名单时只需要改 `clinic.json`：`name_zh`、`aliases`、`languages`、`front_desk_languages`。

| 场景 | 结果 | 发现的问题 → 修法 |
|---|---|---|
| 普通话新患者点名"沈医生" | ✅ 预约成功，前台收到英文摘要 | 原来按英文名匹配，"沈医生"找不到 → `clinic.json` 增加 `aliases`，代码同时匹配中英文名 |
| 中英夹杂改约（老病人 Mei Lin） | ✅ 第二次跑通过 | 第一次 Mei 被听成 May，身份核对失败 → 姓 + 生日 + 来电号码一致时允许名字拼写有出入；姓名颠倒也能认出。生日不对永远不放行 |
| 粤语新患者打流感针 | ✅ 第二次全程粤语 | 第一次回答夹杂普通话 → prompt 明确"粤语不是普通话" |
| 英文点名不收新病人的 Patel 医生 | ✅ 代码挡住，改推沈医生 | — |
| 中文病人一接通先听到英文 | ✅ | 改为中英双语问候 |

另外：给前台的摘要和留言内容一律用英文，并注明来电者说的是什么语言。
仍要注意：Realtime 的粤语明显比普通话弱，粤语病人多的话要用真人重点测；通话末尾偶尔会切回英文。

**还没解决 / 要注意的：**

- 合成语音太干净了。真实电话有噪音、口音、对方中途停顿，ASR 准确率和 VAD 误判一定比这里差。上线前必须用真号码、真人测。
- 一个回合里连着调用多个工具（核对身份 → 注册 → 查空档），每多一轮大约多 1 秒，最长 3–4 秒。可以合并工具或让模型并行调用。
- 中文语音偶尔有用词错误，比如把"疫苗"说成别的词（也可能是测试脚本自己的语音识别听错了）。
- 只靠 prompt 的规则是不稳定的；凡是必须做到的，都要写进代码。

## 7. 真正难的地方（骨架里还没做，面试时主动提）

1. **对接诊所现有的预约系统**——这是最大的未知数，也最容易超工期。诊所多半已经在用某个 EHR / 预约软件（加拿大常见 Jane App、OSCAR、Accuro；美国常见 Athena、eClinicalWorks、Dentrix……）。要么它有 API（把 `scheduling.py` 的 DB 读写换成调它的 API），要么没有（那就是我们的 DB 当主数据，前台在 `/admin` 上看，或者做同步）。**第一个要问的问题。**
2. **合规。** 美国：HIPAA，需要跟 OpenAI、Twilio 都签 BAA（两家都有，但要走企业流程）；加拿大安省：PHIPA。通话录音和转写都是 PHI：要加密存储、设保留期限、控制访问权限；录音前还要告知对方。
3. **姓名 / 生日的识别准确率。** 电话音质 8kHz，名字很容易听错。常见做法：让对方拼一下、复述确认，或者先用来电号码匹配候选人，再用生日确认。
4. **延迟。** 目标是对方说完后 < 1 秒开始回应。speech-to-speech（Realtime）在这点上比 STT→LLM→TTS 三段式有优势；服务器尽量部署在离 OpenAI / Twilio 近的区域（美东）。
5. **评测。** 准备一套场景脚本（预约、改约、听错名字、问医疗问题、急症、要找真人、说中文……），用 `text_chat` 的逻辑做自动回归测试；上线后每周抽查通话摘要。
6. **生产化。** SQLite → Postgres，并加排他约束保证不会重复预约；`/admin` 换成真正的登录；部署（Fly.io / Render / AWS 一台小机器就够）；监控 Twilio 和 OpenAI 的错误率。
7. **外呼**（预约提醒、确认）——后续很自然的扩展，用 Twilio 发起外呼，接到同一套 agent 上。

## 8. 面试时可以问客户的问题

- 现在用什么预约 / 病历系统？有没有 API？
- 一天大概多少通电话？高峰在什么时候？下班后的电话现在怎么处理？
- 来电里预约、改约、问问题、要处方续药的比例大概是多少？
- 需要哪些语言？
- 在美国还是加拿大？对合规（BAA / 录音告知）有什么要求？
- "一两百个客户"指的是病人，还是你们服务的诊所数量？
- 转真人转到哪个号码？没人接的时候怎么办？

## 参考

- OpenAI Realtime conversations: https://developers.openai.com/api/docs/guides/realtime-conversations
- OpenAI Realtime SIP: https://developers.openai.com/api/docs/guides/realtime-sip
- Twilio Elastic SIP Trunking × OpenAI: https://www.twilio.com/en-us/blog/developers/tutorials/product/openai-realtime-api-elastic-sip-trunking
- AI 前台架构概览 (freeCodeCamp): https://www.freecodecamp.org/news/how-ai-receptionists-work-the-architecture-behind-ai-phone-agents/

---

## 开发日志

> 写于 JesusSaysHi 项目期间、关于做电话 agent 这件事的一篇随手笔记

讲道理，我从一开始就根本没想过Specter/Jesus项目需要做打电话。技术上我从来不觉得有难度，因为在人机交互圈子里，给虚拟形象做打电话功能完全是个入门级的东西，推特上天天都能刷到有人自己造轮子实现。-而且都有这么多轮子了，直接用人家轮子不好吗？为什么还要自己写呢？各种开源项目都已经百花齐放到这种程度了，直接用嘛

而且在我这个avatar项目里，完全没有这个必要。何必呢？本来就在电脑屏幕上，天天看着脸不香吗？为什么非要打个电话？打电话不就是给它套个壳？更何况有些电话是纯语音的，连脸都没有。现在这样在电脑屏幕上看着这张脸，随时跟它说话，嘴巴还能动。而且这跟这个项目本来就是违背的。这个项目加语音唤醒、加主动 greeting 之类的, 很大一个原因是我不太乐意去单独做额外的动作：还得摁个快捷键，或者在手机上做个操作，然后才能让它给我打电话。累不累啊，日常的摩擦还不够吗？我都嫌muse还需要我点开看消息而不是直接喊出来这点麻烦了。

我就是想让它 360 度无死角地在这儿等着我，完全不需要额外再加一步去唤醒它

不过最近找工作、投简历的过程中，接连在面试里听到大家提这种打电话、做 dispatcher 的需求。那行吧，上周为了另一场面试顺手准备了一下，结果那场面试完全没用上，一直在问我 RAG 。既然作业做都做了，就顺手在这儿记一遍，免得过两天又忘了。

---

因为当时那场面试是奔着 clinic phone agent 去做准备的，那就顺手把这个作为一个场景，不额外去想别的 scenario 了。

先谈行业里现在一般是怎么做的。这种架构是一个非常 well-defined 的 problem，目前行业里基本上有三条路线：

1. 直接买成品（SaaS）：

   代表产品是各种垂直的 AI receptionists（比如专门针对 dental、medical 的垂直 SaaS，只要是带企业内部资料的 AI receptionist 都在此列）。诊所买完之后，诊所的工程师要做的事情基本就是 configuration 加 integration。这种最适合想省心、完全不想自己接技术团队的诊所，属于全外包方案。（背景交代一下：当时作业的主体是一个影像诊所，也就是 radiologist 相关的，会涉及到一些合规 compliance 要求，但本质是一样的。）
2. 使用 Hosted Voice Agent Platform：

   这类平台现在也做得很垂直了，比如 Vapi、Retell AI、Bland、Synthflow 等。这种模式下，诊所内部的工程师需要做的是：写 prompt，并将 check availability 或 booking 接口作为 webhook 暴露给平台调用。这仍然是一种合作关系，工程师需要写 prompt 并将自家的 schedule calendar 和外部 agent 平台做对接。它的好处是速度极快、排查发现问题也非常快，是 agency 和 contractor 最常用的方案。
3. 完全自己原生开发（Build It Yourself）：

   常见的做法是直接用 Twilio 配合各种 Realtime 模型；如果不那么在乎 latency 或者能解决延迟问题，走传统的 Speech-to-Text -> Text-to-Text -> Text-to-Speech 也行。这种方案下，工程师需要 own the whole thing，从 audio bridging 到 calling 和 logging 全部自己搞定。最适合本身就是在做相关产品的初创团队，或者因合规（compliance）、成本、数据隐私（data）等现实限制，必须做到全部自主掌控的情况。

作为一个起码是中型的企业，真的不要从头自己搞、从头造轮子。去选一家成熟的垂类 Voice Agent Platform 合作，情况就会好很多；如果非要自己从头干，效果不保证且真不知道要干到猴年马月。（不过一般会有这种需求的，其实都是对成本非常敏感，或者有一些别的 compliance 要求。I also feel pity for them）

有个说法是，蒸汽机出现之后，原本不需要蒸汽机的地方也开始用蒸汽机了。现在 Vibe coding 出现之后，大家也开始自己去造原本没需求做的东西。但只要稍微下手去 get hand dirty 就会明白，不管过程中提了多么高大上的专有名词或复杂算法，这部分其实都好办，因为起码已经有 coding agent 帮忙搞定了。

真正麻烦的地方根本不在这儿。比如当前这些项目，难点从来不是把 RAG 串起来，真正的挑战在于让 AI Jesus 像一个人一样去说话去理解发生了什么，它需要做什么tool calling，而且还要贴合具体场景。这其实是一个非常“文艺”而非纯技术层面的事情，效果好与坏高度依赖人类工程师的主观判断（不过倒也不用太悲观，因为 evaluation 是可以提前写的。在这个项目里我确实比较悲观，那是因为 evaluation 也不是我写的。我确实不太懂 gospel，所以我还是选择让 OpenAI 和 Claude Code 分别作为独立且水平起码比我高的角色，让他们来写这个 evaluation。不过在企业中估计没有这个问题吧？企业中总会有这方面的专业人士，总会有懂这个行业的人吧？）。

大家很容易被各种技术名词 overwhelming，只盯着技术那一块，却忽略了实际落地时的其他困难。可能因为我是理工科出身，一开始也容易一叶障目，以为只要搞定这些技术名词事情就顺了。但事实上完全不是这样，落地时会有各种各样乱七八糟的琐事、匪夷所思的口音识别问题把agent引歪了。

对不起，跑题了。接着说：不管用上述哪三条路线，其实它们的架构都是一样的。

最上层是电话网络，往下推流，拿到 phone number 和 audio stream，然后送到语音模型中去。

在语音模型处理的过程中，会涉及 function calling，以及各种 dialog（对话日志）等。Dialog 倒好说，OpenAI 那边把 transcribe 参数打开就行，openai developer platform 平台本身也可以存；不过还有 data, privacy, compliance 的问题，这个就具体 scenario 具体分析了。

语音模型调用的 tool 再去连接自己的 business backend，包括 scheduling rules、patient appointment、各种 message 通知等。这个后端程序作为桥接，再去连诊所当前现有的 EHR 预约软件，或者连他们自己 host 的 database。一般来说，基本上任何一家企业都有些技术积累，哪怕非常老古董，但已经有现成的一套东西在跑了。能不改就尽量不改，除非有非常大的收益，否则尽量不要破坏人家原有的运行逻辑。不然让别人再去学新东西，这种认知负荷和摩擦都是很难克服的。

然后这边我还想吐槽一下。当时为了给拍片子的诊所做准备，我做了一些调研。像这种诊所，必须先收到家庭医生发来的 referral 才能预约，所以当时我准备的难点就在这里。

不得不吐槽一下安省非常混乱的医疗体系：都什么年代了，居然还得发 fax！要给这种有前置条件、必须先拿到 referral 的业务做方案，现实中做integration会遇到很大的问题。而且确实得结合当前医院现有的 IT 系统，先确定对方的 referral。

在医院场景下还有另一个问题：很多检查（比如核磁共振）都有前置准备要求，比如身上不能有心脏起搏器或金属物品，或者前一天需要禁食禁水。如果用户（client）在电话里提出这种医学问题，这里不是说像这种比较普遍、比较固定的问题，而是去问类似："我现在正在……I'm taking some medicine. Then will it be complicated? Should I stop this medicine before it?" 这种非常需要专业知识背景的问题让 AI 去回答是非常不负责任的。Anthropic 那边也强调过 delegation 的重要性：不是要 automate everything，而是要让人和 AI 在 most effective level 上进行 collaboration。

所以给诊所从0做这套东西真的很难，尤其对于我这种根本没有医学背景、全靠现场补课的人来说，肉眼可见的脑袋疼。还是那句话：像医院这种专业机构，麻烦直接去找对应的垂类 SaaS 平台，自家的工程师去做数据脱敏和效果验收。想要自己从零开始做不看现实背景，纯粹是一拍脑门的想法。

备注一下：

RAG 并不只包括 hybrid search 那一块。其实现在只要还需要走几轮、然后拿到信息的，大家都已经 general 地统称为 RAG 了。

所以不管是写 SQL 去查 database、做 function calling，还是用 MCP 或其他方式，只要是从外部获取更多信息（中间内部经过多轮turn），其实这种都算 RAG。之前只觉得 RAG 是语义空间向量检索的那种概念是错的，被教程给教窄了。

回到原话题。如果对方每天的客单量只有一两百个 call，其实一台小服务器加上 SQLite 或者 PostgreSQL 就够了，完全不用上云，不需要队列，不需要 K8s，也不需要 microservices，按照体量办事就行。

我刚才说的都是单租户。如果是多租户的情况（诊所不止一家），数据库那边的表就得改，可以按照被呼叫的号码（也就是 Twilio 的 To 参数）找到对应的诊所配置、prompt 和数据。架构上也得稍微复杂一点，要把流程拆开：之前是一整串下来，现在中间得加一个 router 来做分支。

至于诊所本身的知识，像地址、停车、营业时间、取消政策、保险这些固定的信息，直接塞进 system prompt 就行，没有检索延迟，效果也更好。

这种情况下真正需要检索的是结构化数据，比如 schedule 里的 open slots、预约信息、患者资料等。这些都要通过 tool calling 去查数据库，而不是去检索 knowledge base。

再往下谈就是更细节的代码层面了。整个流程非常清晰：

1. 先用 twilio_stream.py 拿到语音：它主要作为 audio bridge，再加上 barge-in handling。
2. 把音频送进 OpenAI Realtime：以前还要写各种 event loop、transcription 处理什么的，其实现在用 OpenAI Agents SDK 省事很多，完全不用手写那种 while 循环了。
3. 跑 Function Calling：模型那边定下来之后去调 tools.py（写哪都行，假设放这里）。里面主要放 tool definitions 加 dispatch。
4. 调度与数据库存储：再送到另外一个 .py 文件去做 scheduling。这里涉及数据库，所以会再连一个负责 database 的 .py 文件。

数据库选型方面：
虽然 SQLite 或 PostgreSQL 都能写，但考虑并发冲突，量大时 SQLite 并不好使。SQLite 本质上是单文件存储，写入时会锁住整个文件以避免冲突，其他进程就无法访问。如果每天有一两百个 call，SQLite 确实不够看，不管怎样还是 PostgreSQL 更好，它是用事务（transaction）来做 conflict handling。

最后就是处理一些杂事：保存 transcript、写 log、生成 summary 等后续收尾工作。

以及基本上所有这种 dispatch 的场景里面，再怎么着也得给人家留一个人工。那边其实可以再加一条兜底路径：如果实在搞不了，就 transfer to human。人在的情况下直接 transfer 给人工，人不在的情况下就请用户 take message（先留个言）。There's always need a fallback。

还有一个小点：因为这边用了 Twilio，所以还涉及到挂电话的问题。具体是等模型把这句说完，Twilio 那边回传 mark 事件，确定音频真的播完了，再去真正挂断或者转接。

再加上 barge-in 的问题。处理 barge-in 时，用户一开口，就要清掉 Twilio 里面没播完的音频，而且得顺手用 conversation.item.truncate 告诉模型只说到这里。不然模型真的会以为自己把那句话说完了，觉得用户听到了所有内容，并以此接着往下推理。这在实际使用中会造成很多驴唇不对马嘴的问题：比如它本来打算说“B”，但其实没说出来就被打断了，我接着往下问原本的A，它却以为我在问“它刚刚说的B”。

对不起啊，再往前说一下两种电话接入方式。最常用的是这两种：

1. 用默认的 Twilio Media Streams， 这个分 call 的 audio，然后经过我们自己的服务器，去走这个程序然后去发起一个 real-time 的 session
2. 选 OpenAI SIP：

   用 Twilio Elastic SIP Trunk 直接连到 sip:<project-id>@sip.api.openai.com，OpenAI 那边会发 realtime.call.incoming 的 webhook，我们 accept 之后再去挂一个 sideband WebSocket，只处理工具调用。这个的好处是，它不用在我们的服务器上再走一轮了，直接就送到 OpenAI 那边去了，延迟会更低。

然后再谈一下合规性问题吧，反正查都查了，现在说吧。

对于医药这个方面：

1. 美国那边需要遵循的是 HIPAA：

   需要跟 OpenAI、Twilio 都签 BAA，这就得走企业流程。
2. 加拿大安省需要遵循的是 PHIPA：
   - (a) 通话录音和转写都是 PHI（保护健康信息）
   - (b) 需要加密存储，并设置保留期限
   - (c) 需要控制访问权限
   - (d) 录音必须要告知对方
