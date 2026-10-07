export const navItems = [
  { page: "assistant", label: "AI 助手", hint: "默认会话入口", short: "AI", tone: "signal" },
  { page: "tasks", label: "我的任务", hint: "聚合任务、PR 与环境", short: "任", tone: "olive" },
  { page: "knowledge", label: "知识库", hint: "按对象浏览知识源", short: "知", tone: "sand" },
  { page: "skill", label: "Skill", hint: "按任务模板启动", short: "技", tone: "olive" }
];

export const workspaceByPage = {
  assistant: "已登录 · Product Workspace",
  tasks: "已登录 · Task Workspace",
  knowledge: "已登录 · Knowledge Workspace",
  skill: "已登录 · Skill Workspace"
};

export const assistantSkillPrompts = {
  "需求评审": "请先给我一个适合评审会上口头同步的结论，再展开歧义点、风险点和待补规则。",
  "测试点生成": "请根据当前挂载内容，输出一份结构化测试点清单，并标注 P0 / P1 / P2。",
  "代码影响分析": "请结合当前规则和代码上下文，分析受影响的模块、接口、状态流转与回归风险。"
};

export const assistantRecentSessions = [
  { preset: "default", title: "现货杠杆体验金活动评审", subtitle: "需求 + 业务文档 + 代码" },
  { preset: "req-prd-2418", title: "PRD-2418 需求评审", subtitle: "从需求文档发起" },
  { preset: "biz-coupon-rules", title: "优惠券规则澄清", subtitle: "从业务文档发起" },
  { preset: "code-bonus-coupon", title: "bonus-coupon 代码影响分析", subtitle: "从代码范围发起" },
  { preset: "req-prd-3027", title: "PRD-3027 需求梳理", subtitle: "理财需求评审" },
  { preset: "code-appeal-refund", title: "appeal refund-link 回归分析", subtitle: "补偿链路影响判断" },
  { preset: "skill-requirement-review", title: "从 Skill 启动：需求评审", subtitle: "按模板直接进入" }
];

export const assistantPresets = {
  default: {
    title: "现货杠杆体验金活动评审",
    subtitle: "默认会话入口。围绕需求文档、业务文档和代码三类长期范围工作，再按每一轮显式指定的对象继续追问。",
    badges: [
      { tone: "signal", label: "Claude Agent" },
      { tone: "olive", label: "会话范围由应用侧记录" }
    ],
    scopeTitle: "当前会话范围",
    scopeNote: "这里只展示长期挂载的粗粒度范围，不把全部证据单元铺在首屏。",
    contexts: {
      prd: "需求范围: PRD-2418 / 发券与结算",
      biz: "业务域: 营销域 / 优惠券规则",
      code: "代码范围: release/bonus-coupon / coupon/**"
    },
    activeContexts: ["prd", "biz", "code"],
    activeSkill: "需求评审",
    prompt: "请继续展开：如果活动结束后用户订单还未结算，体验金和优惠券状态应该如何处理？",
    messages: [
      {
        role: "user",
        time: "09:42",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [
          { tone: "signal", label: "手动 @需求: PRD-2418" },
          { tone: "sand", label: "页面选择: 营销域 / 优惠券规则" },
          { tone: "olive", label: "页面选择: release/bonus-coupon / coupon/**" }
        ],
        title: "请帮我做一次快速评审",
        detail: "重点看歧义点、风控遗漏，以及测试验证点是否缺失。"
      },
      {
        role: "assistant",
        time: "09:42",
        status: "已读取上下文",
        sections: [
          {
            label: "本轮推断范围",
            sources: [
              { tone: "olive", label: "Skill: 需求评审" },
              { tone: "sand", label: "优惠券状态机" },
              { tone: "olive", label: "coupon/**" }
            ]
          },
          {
            label: "实际读取对象",
            sources: [
              { tone: "signal", label: "PRD-2418 / 发放规则 §4.2" },
              { tone: "sand", label: "营销域 / 优惠券规则 §3.1" },
              { tone: "olive", label: "coupon_service.py" }
            ]
          }
        ]
      },
      {
        role: "assistant",
        time: "09:43",
        status: "回复",
        title: "先给你一个可继续推进的结论",
        detail:
          "当前需求写清了发券入口和资格判断，但没有明确体验金活动与既有优惠券是否互斥，也没有覆盖活动结束后的状态收敛方式。从业务规则和代码实现看，入口校验已经补上，但结算完成前后的状态落点仍然需要更明确的业务定义。",
        bullets: [
          "歧义点：活动结束时未结算订单的体验金和优惠券，是否进入冻结态还是继续等待结算回补。",
          "风控点：结算回调时是否做二次资格校验，以及资格失效后的回滚链路是否完整。",
          "测试点：重复参与、活动结束边界、订单跨天结算、冻结转失效四类场景需要补齐。"
        ],
        sections: [
          {
            label: "来源",
            sources: [
              { tone: "olive", label: "Skill: 需求评审" },
              { tone: "sand", label: "营销域 / 优惠券规则 §3.1" },
              { tone: "olive", label: "coupon_service.py:120-168" }
            ]
          }
        ]
      }
    ]
  },
  "req-prd-2418": {
    title: "基于需求文档发起：PRD-2418 需求评审",
    subtitle: "这个会话由需求文档对象发起，会默认带上关联业务文档和代码范围，直接进入需求评审。",
    badges: [
      { tone: "signal", label: "需求文档发起" },
      { tone: "olive", label: "已挂载推荐 Skill" }
    ],
    scopeTitle: "该会话由需求文档发起",
    scopeNote: "需求是入口对象，业务文档和代码范围作为长期范围自动补足。",
    contexts: {
      prd: "需求文档: PRD-2418 / 现货杠杆体验金活动",
      biz: "关联业务文档: 营销域 / 优惠券规则",
      code: "关联代码: release/bonus-coupon / coupon/**"
    },
    activeContexts: ["prd", "biz", "code"],
    activeSkill: "需求评审",
    prompt: "请把这份需求整理成一份可以直接带去评审会同步的结论。",
    messages: [
      {
        role: "user",
        time: "10:16",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [{ tone: "signal", label: "需求文档入口: PRD-2418" }],
        title: "从需求文档直接开始评审",
        detail: "我先要一个适合会上同步的结论，再给我补歧义点和风险点。"
      },
      {
        role: "assistant",
        time: "10:17",
        status: "回复",
        title: "需求本身已经够开会，但边界规则还需要补充",
        detail:
          "需求目标、资格和主流程已经完整，可直接开始评审。真正影响结论的是活动结束边界、异常回滚和互斥规则目前仍然偏弱，建议在评审会中优先锁定这三类问题。",
        bullets: [
          "优先补活动结束后的未结算订单状态。",
          "明确补偿失败与人工处理链路。",
          "写清体验金券与其他优惠券的互斥优先级。"
        ],
        sections: [
          {
            label: "来源",
            sources: [
              { tone: "signal", label: "PRD-2418 §2 / §4" },
              { tone: "sand", label: "优惠券规则 §3.1" }
            ]
          }
        ]
      }
    ]
  },
  "biz-coupon-rules": {
    title: "基于业务文档发起：优惠券规则澄清",
    subtitle: "从业务文档进入对话时，长期范围以领域规则为核心，适合先做概念对齐和状态澄清。",
    badges: [
      { tone: "sand", label: "业务文档发起" },
      { tone: "olive", label: "领域规则优先" }
    ],
    scopeTitle: "当前会话范围",
    scopeNote: "这场会话默认围绕营销域 / 优惠券规则工作，只在需要时再补需求与代码证据。",
    contexts: {
      biz: "业务文档: 营销域 / 优惠券规则",
      prd: "关联需求: PRD-2418",
      code: "关联代码: release/bonus-coupon / coupon/**"
    },
    activeContexts: ["biz", "prd"],
    activeSkill: "需求评审",
    prompt: "帮我澄清一下活动结束时未结算订单对应的券到底应该停在哪个状态。",
    messages: [
      {
        role: "user",
        time: "11:02",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [
          { tone: "sand", label: "业务文档入口: 优惠券规则" },
          { tone: "signal", label: "补充需求: PRD-2418" }
        ],
        title: "活动结束边界要怎么讲才不会有歧义？",
        detail: "我想确认冻结、失效和回补三者的边界。"
      },
      {
        role: "assistant",
        time: "11:03",
        status: "回复",
        title: "推荐把它讲成“活动结束不等于状态终结”",
        detail:
          "业务规则已经暗示活动结束后仍可能等待订单结算，因此冻结是中间态，真正终态需要以结算结果或补偿任务为准。这样表达能同时覆盖正向结算和异常补发。",
        bullets: [
          "活动结束只是停止新领取，不直接结束已发券生命周期。",
          "未结算订单对应的券先冻结，等待结算终态回传。",
          "补偿失败进入人工审核，不要直接写成失效。"
        ]
      }
    ]
  },
  "code-bonus-coupon": {
    title: "基于代码范围发起：bonus-coupon 影响分析",
    subtitle: "从代码范围发起会话时，先落到实现对象，再反查需求和业务文档是否支撑得住当前逻辑。",
    badges: [
      { tone: "olive", label: "代码范围发起" },
      { tone: "signal", label: "影响分析模式" }
    ],
    scopeTitle: "当前会话范围",
    scopeNote: "代码范围是长期上下文，其余文档只在追问时增量读取。",
    contexts: {
      code: "代码范围: release/bonus-coupon / coupon/**",
      biz: "关联业务文档: 营销域 / 优惠券规则",
      prd: "关联需求: PRD-2418"
    },
    activeContexts: ["code", "biz"],
    activeSkill: "代码影响分析",
    prompt: "请分析如果补偿链路改成异步任务后，哪些模块和回归点会受到影响。",
    messages: [
      {
        role: "user",
        time: "15:18",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [{ tone: "olive", label: "代码范围入口: release/bonus-coupon / coupon/**" }],
        title: "补偿链路异步化之后要回归哪些地方？",
        detail: "希望先按模块拆一下影响面。"
      },
      {
        role: "assistant",
        time: "15:19",
        status: "回复",
        title: "重点会落在状态流转、任务补偿和用户可见状态一致性",
        detail:
          "异步化后，主链路代码量未必变多，但状态延迟和补偿失败重试会成为新的回归焦点。需要同时关注服务层、任务调度、运营后台展示和用户端状态同步。",
        bullets: [
          "服务层：`coupon_service` 与状态更新入口要支持延迟终态。",
          "任务层：补偿任务的重试、幂等和失败告警要补齐。",
          "前后台：用户可见状态与运营后台状态不能出现短时矛盾。"
        ]
      }
    ]
  },
  "req-prd-3027": {
    title: "基于需求文档发起：PRD-3027 需求梳理",
    subtitle: "围绕 Launchpool 改版的术语、状态和收益规则先做一次结构化梳理。",
    badges: [
      { tone: "signal", label: "需求文档发起" },
      { tone: "sand", label: "理财域" }
    ],
    scopeTitle: "该会话由需求文档发起",
    scopeNote: "默认先围绕 PRD-3027 这份需求文档工作，必要时再补业务规则和代码范围。",
    contexts: {
      prd: "需求文档: PRD-3027 / Launchpool 改版",
      biz: "关联业务文档: 理财域 / Launchpool 结算说明",
      code: "关联代码: feature/launchpool-revamp"
    },
    activeContexts: ["prd", "biz"],
    activeSkill: "需求评审",
    prompt: "请先把这份改版需求里最容易讲不清的术语边界梳理出来。",
    messages: [
      {
        role: "user",
        time: "16:08",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [{ tone: "signal", label: "需求文档入口: PRD-3027" }],
        title: "先帮我把这份需求里的术语和状态讲顺",
        detail: "我想优先看锁仓、快照、收益发放、赎回这几个概念有没有混淆。"
      },
      {
        role: "assistant",
        time: "16:09",
        status: "回复",
        title: "这份需求可读性不错，但几个核心术语的边界还不够硬",
        detail:
          "快照余额、当前持仓和可赎回份额是三个不同维度，现在文案里有交叉表述。建议先把术语固定，再写状态和收益规则，不然测试口径会跟着漂移。",
        bullets: [
          "快照余额只用于收益计算，不应直接当作页面实时持仓。",
          "赎回能力与收益结算时点需要拆开表达，避免用户误解。",
          "页面状态不要创造后台未定义的混合态。"
        ]
      }
    ]
  },
  "code-appeal-refund": {
    title: "基于代码范围发起：appeal refund-link 回归分析",
    subtitle: "从补偿链路实现对象进入会话，先看接口联动、任务重试和用户可见状态一致性。",
    badges: [
      { tone: "olive", label: "代码范围发起" },
      { tone: "sand", label: "回归分析" }
    ],
    scopeTitle: "当前会话范围",
    scopeNote: "这场会话先围绕 appeal/refund-link 的实现范围工作，需要时再补业务规则与上游需求。",
    contexts: {
      code: "代码范围: appeal/refund-link",
      biz: "关联业务文档: 申诉赔付规则",
      prd: "关联需求: 退款链路优化"
    },
    activeContexts: ["code", "biz"],
    activeSkill: "代码影响分析",
    prompt: "请分析 refund-link 补偿逻辑调整后，任务链路和运营后台会受哪些影响。",
    messages: [
      {
        role: "user",
        time: "17:02",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [{ tone: "olive", label: "代码范围入口: appeal/refund-link" }],
        title: "补偿链路改动后我要重点回归哪里？",
        detail: "希望先按任务链路、后台展示、用户可见状态三个方向拆开。"
      },
      {
        role: "assistant",
        time: "17:03",
        status: "回复",
        title: "重点不是接口本身，而是补偿失败后的状态一致性",
        detail:
          "refund-link 这类补偿逻辑最怕主链成功、补偿失败、展示未同步三者错位。建议把任务重试、人工介入入口和运营后台状态说明一起回归。",
        bullets: [
          "任务链路：重试、超时、幂等与告警必须一起验证。",
          "后台展示：运营后台状态字段要和补偿任务真实状态一致。",
          "用户侧：失败中的中间态要有可解释文案，不要直接显示终态。"
        ]
      }
    ]
  },
  "skill-requirement-review": {
    title: "从 Skill 启动：需求评审",
    subtitle: "按需求评审 Skill 直接进入会话，适合先拿到会上可同步的结论，再补歧义点和风险点。",
    badges: [
      { tone: "olive", label: "Skill 启动" },
      { tone: "signal", label: "需求评审模板" }
    ],
    scopeTitle: "当前会话范围",
    scopeNote: "这场会话先以 Skill 输出结构为主，文档与代码对象在需要时再挂载进来。",
    contexts: {
      prd: "待补需求文档: 当前未固定对象",
      biz: "待补业务文档: 当前未固定对象",
      code: "可选代码范围: 当前未固定对象"
    },
    activeContexts: ["prd", "biz"],
    activeSkill: "需求评审",
    prompt: "请按需求评审的结构，先给我一版适合会上同步的结论模板。",
    messages: [
      {
        role: "user",
        time: "17:26",
        status: "提问",
        sectionLabel: "本轮新增指定",
        sources: [{ tone: "olive", label: "Skill 入口: 需求评审" }],
        title: "先按 Skill 模板给我一个评审输出框架",
        detail: "我稍后再补具体需求对象，现在先要输出结构。"
      },
      {
        role: "assistant",
        time: "17:27",
        status: "回复",
        title: "可以先按“结论、歧义点、风险点、待补充项”四段来组织",
        detail:
          "如果对象还没挂载，先输出一个稳定框架最合适。等具体需求、业务规则或代码范围补进来后，再把每一段填实。",
        bullets: [
          "结论：先说这份需求能不能进评审，核心决策点是什么。",
          "歧义点：集中列边界、状态和异常定义不清的地方。",
          "风险点：补偿、权限、风控、历史数据兼容等高风险项单独列出。"
        ]
      }
    ]
  }
};

export const knowledgeBases = {
  requirements: {
    label: "需求文档",
    navHint: "同步需求、PRD、验收口径",
    heroTitle: "知识库先归入主菜单，再在目录树里浏览真实对象",
    heroCopy:
      "当前展示的是需求文档知识树。左侧目录树直接对应同步后的真实文件组织，用户不需要先理解抽象分类，只要切到对应子菜单，就能像浏览文档目录一样找到目标对象。",
    metrics: [
      { value: "2", label: "已同步需求文档" },
      { value: "2", label: "目录层级" },
      { value: "1", label: "默认启动动作" }
    ],
    defaultNodeId: "req-prd-2418",
    tree: {
      id: "req-root",
      kind: "folder",
      name: "requirements",
      title: "requirements",
      summary: "同步需求总目录",
      path: "workspace/knowledge/requirements",
      updatedAt: "2026-04-11 18:20",
      cards: [
        { title: "目录职责", body: "保留需求原始结构与基础元信息，让用户先看到真实需求文件。" },
        { title: "组织方式", body: "先按同步来源分组，再按业务域沉淀，既能追溯来源，也能按场景浏览。" }
      ],
      related: [
        { tone: "signal", label: "可关联业务文档" },
        { tone: "olive", label: "可关联代码范围" }
      ],
      children: [
        {
          id: "req-clickup",
          kind: "folder",
          name: "clickup",
          summary: "来自 ClickUp 的同步需求目录",
          updatedAt: "2026-04-11 18:20",
          children: [
            {
              id: "req-marketing",
              kind: "folder",
              name: "marketing",
              summary: "营销相关需求目录",
              updatedAt: "2026-04-11 18:20",
              children: [
                {
                  id: "req-prd-2418",
                  kind: "file",
                  type: "requirements",
                  name: "PRD-2418.md",
                  title: "PRD-2418 / 现货杠杆体验金活动",
                  summary: "活动目标、资格规则、状态边界和验收要求",
                  path: "workspace/knowledge/requirements/clickup/marketing/PRD-2418.md",
                  updatedAt: "2026-04-11 18:20",
                  owner: "营销产品",
                  status: "已同步",
                  stats: ["ClickUp", "3 个关键规则", "关联 1 个代码范围"],
                  previewCards: [
                    { title: "需求目标", body: "提升现货杠杆新客转化，通过体验金券引导首次参与，并控制重复领取与资格滥用。" },
                    { title: "关键规则", body: "资格校验覆盖新客身份、活动时间窗、参与次数限制以及领取后的状态收敛要求。" },
                    { title: "验收要点", body: "覆盖领取成功、冻结、核销、活动结束后未结算订单等关键场景。" }
                  ],
                  excerptLabel: "需求摘录",
                  excerpt:
                    "1. 用户领取体验金后，需进入统一的优惠券生命周期，不允许脱离状态机独立流转。\n2. 活动结束时，如存在未结算订单，需明确体验金券状态如何收敛。\n3. 需要补充异常回滚、补发策略以及重复参与限制的验收说明。",
                  related: [
                    { tone: "sand", label: "关联业务文档: 营销域 / 优惠券规则" },
                    { tone: "olive", label: "关联代码: release/bonus-coupon / coupon/**" },
                    { tone: "signal", label: "推荐 Skill: 需求评审" }
                  ],
                  launchTitle: "基于该需求发起会话",
                  launchDescription: "自动挂载当前需求文档，并补齐高相关业务文档与推荐 Skill。",
                  preset: "req-prd-2418"
                }
              ]
            },
            {
              id: "req-earn",
              kind: "folder",
              name: "earn",
              summary: "理财相关需求目录",
              updatedAt: "2026-04-10 14:05",
              children: [
                {
                  id: "req-prd-3027",
                  kind: "file",
                  type: "requirements",
                  name: "PRD-3027.md",
                  title: "PRD-3027 / Launchpool 改版",
                  summary: "聚焦锁仓、快照、收益发放和赎回体验的改版",
                  path: "workspace/knowledge/requirements/clickup/earn/PRD-3027.md",
                  updatedAt: "2026-04-10 14:05",
                  owner: "理财产品",
                  status: "已同步",
                  stats: ["ClickUp", "4 个术语边界", "关联 1 个代码范围"],
                  previewCards: [
                    { title: "需求目标", body: "统一改版后的页面文案、状态展示和收益结算口径。" },
                    { title: "改动重点", body: "锁仓、快照、收益发放、赎回的概念定义需要与后台结算逻辑一致。" }
                  ],
                  excerptLabel: "需求摘录",
                  excerpt:
                    "1. 快照余额用于收益计算，当前持仓用于页面展示。\n2. 赎回是否影响当日收益，需要结合快照时点明确口径。\n3. 页面状态不得新增后台未定义的混合态。",
                  related: [
                    { tone: "sand", label: "关联业务文档: 理财域 / Launchpool 结算说明" },
                    { tone: "olive", label: "关联代码: feature/launchpool-revamp" }
                  ],
                  launchTitle: "基于该需求发起会话",
                  launchDescription: "适合先生成测试点或核对术语边界。",
                  preset: "default"
                }
              ]
            }
          ]
        }
      ]
    }
  },
  business: {
    label: "业务文档",
    navHint: "领域规则、术语、状态机",
    heroTitle: "业务文档作为知识库子菜单，直接以领域目录树呈现",
    heroCopy:
      "当前展示的是业务文档知识树。目录层先按业务域分组，再落到具体规则文档，用户进入知识库后就能像查阅真实知识库一样浏览规则。",
    metrics: [
      { value: "3", label: "领域目录" },
      { value: "3", label: "示例文档" },
      { value: "1", label: "目录入口" }
    ],
    defaultNodeId: "biz-coupon-rules",
    tree: {
      id: "biz-root",
      kind: "folder",
      name: "business-docs",
      title: "business-docs",
      summary: "业务文档总目录",
      path: "workspace/knowledge/business-docs",
      updatedAt: "2026-04-11 09:40",
      cards: [
        { title: "目录职责", body: "承载跨需求复用的领域规则、状态机与术语口径。" },
        { title: "浏览方式", body: "先看领域目录，再展开规则文档，符合知识库常见的树形心智。" }
      ],
      children: [
        {
          id: "biz-marketing",
          kind: "folder",
          name: "marketing",
          summary: "营销域文档目录",
          updatedAt: "2026-04-11 09:40",
          children: [
            {
              id: "biz-coupon-rules",
              kind: "file",
              type: "business",
              name: "coupon-rules.md",
              title: "营销域 / 优惠券规则",
              summary: "沉淀优惠券生命周期、资格约束和异常回补规则",
              path: "workspace/knowledge/business-docs/marketing/coupon-rules.md",
              updatedAt: "2026-04-11 09:40",
              owner: "交易增长 / 营销运营",
              status: "已同步",
              stats: ["业务规则", "状态机", "关联 1 个需求 + 1 个代码范围"],
              previewCards: [
                { title: "规则范围", body: "覆盖领取、待激活、冻结、核销、失效、补发等核心状态。" },
                { title: "状态机", body: "活动结束不等于券直接失效，若订单未结算则需等待终态回传或补偿任务收敛。" },
                { title: "业务约束", body: "资格校验、活动时间窗、补发策略和异常回滚链路都需要一致。" }
              ],
              excerptLabel: "文档摘录",
              excerpt:
                "1. 用户领取成功后进入待激活，不允许直接进入可使用状态。\n2. 活动结束时未结算订单对应的券，需跟随结算终态做冻结收敛。\n3. 补偿任务失败时，需进入人工审核队列并保留用户可见状态说明。",
              related: [
                { tone: "signal", label: "关联需求: PRD-2418" },
                { tone: "olive", label: "关联代码: release/bonus-coupon / coupon/**" },
                { tone: "sand", label: "推荐 Skill: 需求评审" }
              ],
              launchTitle: "基于该业务文档发起会话",
              launchDescription: "直接围绕领域规则进入澄清型会话，适合先锁边界再回看需求。",
              preset: "biz-coupon-rules"
            }
          ]
        }
      ]
    }
  },
  code: {
    label: "代码",
    navHint: "仓库、分支、路径范围",
    heroTitle: "代码知识按仓库与路径范围组织，树里直接看到实现对象",
    heroCopy:
      "当前展示的是代码知识树。目录层先到仓库和分支，再落到具体模块与文件，用户可以直接从实现对象发起影响分析，而不是先理解抽象索引术语。",
    metrics: [
      { value: "1", label: "仓库样例" },
      { value: "3", label: "关键目录" },
      { value: "1", label: "默认入口" }
    ],
    defaultNodeId: "code-coupon-service",
    tree: {
      id: "code-root",
      kind: "folder",
      name: "code",
      title: "code",
      summary: "代码知识总目录",
      path: "workspace/knowledge/code",
      updatedAt: "2026-04-12 09:15",
      cards: [
        { title: "目录职责", body: "把仓库、分支和关键路径显式展示出来，便于直接选中实现范围。" },
        { title: "使用方式", body: "先进入仓库，再看分支与模块，适合从实现视角反查业务规则。" }
      ],
      children: [
        {
          id: "code-bonus-repo",
          kind: "folder",
          name: "bonus-coupon-repo",
          summary: "营销活动券相关仓库",
          updatedAt: "2026-04-12 09:15",
          children: [
            {
              id: "code-release-branch",
              kind: "folder",
              name: "release/bonus-coupon",
              summary: "当前活动分支",
              updatedAt: "2026-04-12 09:15",
              children: [
                {
                  id: "code-coupon-module",
                  kind: "folder",
                  name: "coupon",
                  summary: "券相关核心模块",
                  updatedAt: "2026-04-12 09:15",
                  children: [
                    {
                      id: "code-coupon-service",
                      kind: "file",
                      type: "code",
                      name: "coupon_service.py",
                      title: "coupon_service.py",
                      summary: "处理发券、冻结、核销和补偿回补的核心服务文件",
                      path: "workspace/knowledge/code/bonus-coupon-repo/release/bonus-coupon/coupon/coupon_service.py",
                      updatedAt: "2026-04-12 09:15",
                      owner: "增长工程",
                      status: "已索引",
                      stats: ["Python", "状态流转核心", "关联 1 个需求 + 1 个业务文档"],
                      previewCards: [
                        { title: "核心职责", body: "统一承接发券、状态迁移、结算回调和补偿任务收敛。" },
                        { title: "风险点", body: "异步回调与补偿链路共同作用时，最容易出现状态重复写入和用户可见状态漂移。" },
                        { title: "联动关系", body: "与优惠券规则和 PRD-2418 强关联，适合直接进入影响分析。" }
                      ],
                      excerptLabel: "代码摘录",
                      excerpt:
                        "def settle_coupon(coupon_id, order_status):\n    if order_status == 'pending':\n        return freeze_coupon(coupon_id)\n    if order_status == 'settled':\n        return consume_coupon(coupon_id)\n    return rollback_coupon(coupon_id)",
                      related: [
                        { tone: "signal", label: "关联需求: PRD-2418" },
                        { tone: "sand", label: "关联业务文档: 优惠券规则" },
                        { tone: "olive", label: "推荐 Skill: 代码影响分析" }
                      ],
                      launchTitle: "基于该代码范围发起会话",
                      launchDescription: "直接围绕核心实现对象分析模块影响、状态链路与回归建议。",
                      preset: "code-bonus-coupon"
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }
  }
};

export const skillTree = {
  id: "skills-root",
  kind: "folder",
  name: "skills",
  title: "skills",
  summary: "Skill 总目录",
  path: "workspace/.claude/skills",
  updatedAt: "2026-04-12 10:32",
  children: [
    {
      id: "skills-product",
      kind: "folder",
      name: "product",
      summary: "产品相关 Skill",
      updatedAt: "2026-04-12 10:25",
      children: [
        {
          id: "skill-requirement-review",
          kind: "file",
          name: "requirement-review.md",
          title: "需求评审",
          path: "workspace/.claude/skills/product/requirement-review.md",
          updatedAt: "2026-04-12 10:20",
          commandHint: "@skill requirement-review",
          launchPreset: "req-prd-2418",
          tags: ["产品", "评审", "会议准备"],
          summary: "把需求、业务规则和可选代码范围整理成会上可直接同步的结论。",
          content: `# 需求评审

## 适用场景
- 需求评审会前
- 活动规则改版
- 文档补充验收口径

## 默认输入
- 需求文档
- 业务规则文档
- 如有需要可补充代码范围

## 输出结构
1. 结论
2. 歧义点
3. 风险点
4. 待补充项

## 执行要点
- 先判断范围和目标是否清楚
- 再核对规则、状态和异常收敛
- 最后给出可以直接带去评审会的清单`
        },
        {
          id: "skill-test-generator",
          kind: "file",
          name: "test-generator.md",
          title: "测试点生成",
          path: "workspace/.claude/skills/product/test-generator.md",
          updatedAt: "2026-04-12 10:25",
          commandHint: "@skill test-generator",
          launchPreset: "default",
          tags: ["产品", "测试", "提测前"],
          summary: "按主流程、异常流程和边界条件输出结构化测试点清单。",
          content: `# 测试点生成

## 适用场景
- 提测前梳理测试点
- 需求变更后的回归分析
- 接口或状态流改动后的边界覆盖

## 默认输入
- 需求文档
- 业务规则
- 可选代码分支或接口说明

## 输出结构
1. 主流程测试点
2. 异常流程测试点
3. 边界条件测试点
4. 优先级标记

## 执行要点
- 先抽状态和关键节点
- 再补失败、超时、回滚、重复操作
- 输出可直接复制到测试单的清单`
        }
      ]
    },
    {
      id: "skills-engineering",
      kind: "folder",
      name: "engineering",
      summary: "工程相关 Skill",
      updatedAt: "2026-04-12 10:28",
      children: [
        {
          id: "skill-impact-analysis",
          kind: "file",
          name: "impact-analysis.md",
          title: "代码影响分析",
          path: "workspace/.claude/skills/engineering/impact-analysis.md",
          updatedAt: "2026-04-12 10:28",
          commandHint: "@skill impact-analysis",
          launchPreset: "code-bonus-coupon",
          tags: ["工程", "回归", "影响面"],
          summary: "从规则变化反推实现改动和回归风险，适合接口或状态链路变更。",
          content: `# 代码影响分析

## 适用场景
- 规则改动后的代码回归判断
- 接口字段变化评估
- 补偿流程和异步任务联动分析

## 默认输入
- 代码分支
- 接口说明
- 规则或业务文档

## 输出结构
1. 影响模块
2. 影响接口或字段
3. 状态流转变化
4. 回归建议

## 执行要点
- 从规则或需求变化反推模块
- 同时检查接口、后台、任务链路
- 结果必须落到可执行回归范围`
        }
      ]
    },
    {
      id: "skills-shared",
      kind: "folder",
      name: "shared",
      summary: "共享规范",
      updatedAt: "2026-04-12 10:32",
      children: [
        {
          id: "skill-style-guide",
          kind: "file",
          name: "style-guide.md",
          title: "Skill 编写规范",
          path: "workspace/.claude/skills/shared/style-guide.md",
          updatedAt: "2026-04-12 10:32",
          commandHint: "docs only",
          launchPreset: "default",
          tags: ["规范", "共享"],
          summary: "统一 Skill 的文件结构、表达粒度和界面映射方式。",
          content: `# Skill 编写规范

## 文件结构
- 标题
- 适用场景
- 默认输入
- 输出结构
- 执行要点

## 编写原则
- 先说这个 Skill 是做什么的
- 再说需要哪些输入
- 再说会产出什么
- 避免大段解释性废话
- 能写成短段落，就不要堆标签和头部说明

## 界面原则
- 左侧目录树找文件
- 右侧直接看文件内容
- 编辑动作应就近出现`
        }
      ]
    }
  ]
};
