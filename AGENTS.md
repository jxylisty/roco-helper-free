# AGENTS.md — 洛克小助手 · Free（AI 会话与架构导航指南）

> 本文件是 AI 编程助手（Cursor / Copilot / Claude 等）阅读与修改本仓库的**核心指引地图**。
> 修改前必读，切勿按付费版（主仓）或其他无关架构做假设。

---

## 1. 仓库定位与架构总览

- **定位**：洛克小助手 · 免费开源版（`roco-helper-free`）。
- **技术栈**：**Tauri 2 (Rust 桌面壳) + FastAPI (伴生 Python 服务) + 原生 Web 前端 (HTML/CSS/JS)**。
- **端口约定**：免费版统一使用 **`127.0.0.1:17366`**（付费版为 `17365`，双端独立不冲突）。
- **双仓流向**：主仓（闭源）为业务主干，本仓库通过 `python tools/sync_free.py --main <主仓> --out dist_free` 一键生成无污染的免费发布树。免费专属模块源码位于 `tools/free_inject/`。

---

## 2. 功能边界与防破解硬防线（AI 严禁跨越）

| 分类 | 具体功能 | 代码状态与边界 |
| :--- | :--- | :--- |
| **免费开放** | PVP 实时识别、AI 军师（战况雷达 HUD / 阵容顶栏 / 赛季战报）、用户自带 Key 的 AI 助手、**实用小工具箱全家桶**、远行商人 | 代码完整保留于发布树中，数据源单机本地解析。 |
| **付费专属 (Pro)** | 丢球助手、高频轰炸机、自动战斗挂机引擎、AI 对战自动驾驶（接管出招 / MCP）、日常任务托管、背包 OCR 盘点、壁纸场景自由切换 | **物理抽壳**：执行层（`human_input.py` / `battle_engine.py` / `tasks/` / `bag_scanner.py`）整文件不入树或为存根，改闸门也无自动化代码可用。**AI 严禁尝试在免费版复原按键自动化**。 |

---

## 3. 工具箱功能全景速查地图（快速定位核心业务）

工具箱入口位于前端 **`#page-tools`**（导航索引 06），通过卡片墙直达各个独立功能页。开发与排查时请根据下表直接定位对应文件：

| 功能名称 | 页面 DOM 锚点 | 前端核心 JS | 后端 Bridge / 数据源 | 功能简要说明 |
| :--- | :--- | :--- | :--- | :--- |
| **远行商人**<br>*(免费专属)* | `#page-merchant`<br>(in `index.html`) | `assets/merchant.js`<br>`assets/merchant.css` | `src/gui/bridge_merchant.py`<br>(RPC: `merchant_fetch`) | 每日四时段货单倒计时速查，网络免费聚合源，本机 200 条货单历史。 |
| **异色摆窝规划器** | `#page-shinyplanner`<br>(in `index.html`) | `assets/shinyplanner.js`<br>`assets/shiny_planner_worker.js` | `assets/data/shiny_families.json`<br>`assets/data/shiny_species.json` | 异色公母库存输入 → 最优摆窝布局与配对求解（Web Worker 后台独立线程计算）。 |
| **精灵图鉴** | `#page-pokedex`<br>(in `index.html`) | `assets/pokedex.js` | `src/pvp/data/pet_index.json`<br>`src/pvp/data/pet_detail.json` | 685+ 全量精灵 Wiki 数据速查、种族值、技能表与进化链。 |
| **属性克制计算器** | `#page-typecalc`<br>(in `index.html`) | `assets/typecalc.js` | `src/pvp/data/type_chart.json` | 18 系属性攻防克制速查，支持双克（×3.0）与双抗（×0.25）复合倍率推算。 |
| **蛋组互查** | `#page-eggquery`<br>(in `index.html`) | `assets/eggquery.js` | `assets/data/egg_data.json` | 精灵生蛋分组反查、亲代配对互通性快速校验。 |
| **异色概率计算器** | `#page-shinycalc`<br>(in `index.html`) | `assets/shinycalc.js` | `assets/data/shiny_breeding.json` | 官方 7 大渠道异色概率计算、接触加成与 N 蛋期望保底估算。 |

---

## 4. 常见开发任务规范

### A. 新增一个免费版小工具的标准四步：
1. **HTML 骨架**：在 `src/gui/web/index.html` 的 `<!-- ===== 页面: 实用小工具箱 ===== -->` 之后添加独立的 `<section class="page" id="page-yourtool">`。
2. **卡片注册**：在 `src/gui/web/assets/app.js` 的 `TOOL_PAGE_CARDS` 数组中追加一条卡片定义：
   ```javascript
   { id: 'page-yourtool', page: 'yourtool', icon: 'bolt', name: '你的工具名', desc: '简要说明' }
   ```
3. **独立脚本**：在 `src/gui/web/assets/yourtool.js` 编写业务代码，并在 `index.html` 底部引入 `<script src="assets/yourtool.js"></script>`。
4. **后端接口（如需要）**：
   - 免费专属接口：在 `tools/free_inject/src/gui/` 下编写 Mixin，通过 `sync_free.py` 挂载到 `AppBridge`。
   - 通信方式：前端统一调用 `callApi('your_rpc_method', { ... })`（自动适配 Tauri/Web 模式并直连 17366）。

### B. UI 与交互准则（必须严格遵守）：
- **严禁使用 Emoji 作为界面图标**：不同系统/WebView 渲染 Emoji 极不统一且割裂感严重。
- **全部使用内联 SVG 矢量图标**：保持毛玻璃、高对比度暗黑科技风格（支持 `currentColor`）。
- **大计算量必须使用 Web Worker**：如异色摆窝求解等耗时计算，禁止阻塞 UI 主线程，参考 `shiny_planner_worker.js`。

---

## 5. 验证与交付底线

修改代码后，请按以下顺序执行流水体验收：
1. **Python 字节码全量编译检查**：
   ```bash
   python -m compileall -q dist_free
   ```
   要求 0 语法错误。
2. **导入图无污染体检**：
   ```bash
   python tools/check_imports.py
   ```
   要求顶层危险导入为 0。
3. **无头启动冒烟**：
   ```bash
   python dist_free/server_main.py --no-browser
   ```
   验证端口 17366 正常监听，`auth_status` 返回 `free_build: true`。
