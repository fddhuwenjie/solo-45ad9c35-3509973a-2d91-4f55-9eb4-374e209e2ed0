# 钣金折弯工艺规划 REST API

Python + FastAPI + Pydantic + SQLite。逐步推算成形几何，确定性搜索可行折弯顺序，
逐步核对最小翻边、模具适配、吨位、行程/闭合高度、工件与机架/刀具碰撞、后挡料可达
与已成形边遮挡。JSON 工艺卡与逐步 SVG 侧视图共用同一份几何结果。

## 运行

```bash
pip install -r requirements.txt
python -m app            # 或 uvicorn app.main:app --port 8000
# 数据库默认 /tmp/bendplan.db，可用 BENDPLAN_DB 覆盖
```

启动后访问 `/docs`。健康检查：`GET /health`。

## 目录与流程

1. `POST /parts` — 建立工件（轮廓、折弯线、厚度、内半径、目标角、纹理、机床、
   候选上下模）。目录引用会立即校验。
2. `POST /parts/{id}/solve` — 自动求解，生成一张工艺卡（draft）。
3. `POST /parts/{id}/check-sequence` — 校验技师给定顺序（不落库）。
4. `GET /cards/{card_id}` / `.../svg` — 工艺卡 JSON 与逐步侧视图（`step=0`
   返回含全部步骤的 HTML 页）。
5. `POST /cards/{card_id}/seal` — 签发封存；封存后不可改。
6. `POST /cards/{card_id}/branch` — 尺寸或设备变化时从封存单分支重算（新版本，
   记录 parent）。无变化会被拒绝。
7. `POST /cards/{card_id}/first-piece` — **首件回弹校正**：向已封存工艺卡提交
   逐折弯实测角（另含测量时刻、量具、材料批次、实测厚度）；按材料/厚度带/纹理
   关系/目标角/模具组合/折弯方向取可比样本，中位数+MAD 稳健统计给出逐折弯过压
   补偿，门限全过时从原卡派生一张 **draft**（不自动签发）。
8. `GET /cards/{card_id}/first-piece`、`GET /first-piece/{run_id}` — 只读测量
   记录；`GET /cards/{card_id}/lineage`、`GET /parts/{id}/cards` — 历史版本，
   校正派生卡的 lineage 节点标明 `correction_kind` 与首件 run。

目录：`GET /catalog/{materials,dies,punches,presses}`（启动时内置 DC04/SUS304、
V12/V16/V24 凹模、R2 鹅颈/R3/R5 冲头、100t 与 50t 折弯机）。

## 每步输出

折弯线、工件朝向、进料方向、是否翻面、凹模/冲头、后挡料位置与接触边、预计吨位、
折弯/过压角度，以及全部核对项（`ok=true/false`）。无解时 `failure` 给出**最早失败
步骤**、该折弯，以及共同造成失败的约束代码集合（如 `min_flange`、`tonnage`、
`frame_collision`、`tool_collision`、`backgauge_reach`、`backgauge_occlusion`）。

## 首件回弹校正

同一牌号换卷或实测厚度变化时回弹会漂移，而工件只有一个名义 `springback_deg`。
首件下线后向**已封存且可行**的工艺卡提交测量：每条记录必须对应原步骤，且
`bend_id / die_id / punch_id / flip` 与封存步骤完全一致（折弯方向不符 400），
另附 `measured_at`、`instrument`、`material_lot`、`measured_thickness_mm`；
`idempotency_key` 唯一，重放返回 `replay=true` 的既有记录，不重复入库。

样本池取全部历史首件测量，逐折弯按 **材料、名义厚度 ±15% 带、纹理关系
（折弯轴与轧纹平行/垂直）、目标角 ±5°、上下模组合、折弯方向** 过滤（本次首件
本身也入池）。统计量为观测回弹中位数与稳健散度 `1.4826·MAD`；观测回弹 =
实测夹角 − 目标夹角 + 当时采用的过压量。以下任一情况只回 **建议与原因**，不建
卡：样本数 < 3（可在请求 `thresholds` 中配置）、实测厚度偏离名义 > 20%
（工况不相容）、稳健 σ > 1.5°（离散度超限）、|中位补偿| > 6°（超可配置上限）。

门限全过时以 `springback_overrides`（逐折弯）从原卡输入派生新工件、强制原折弯
顺序且只允许原卡实际使用的上下模，重跑规划——重查冲头角度、行程、闭合高度与
滑块下行摆动碰撞；若派生计划不可行或模具/方向发生漂移，同样只回原因。派生卡为
**draft**（version+1、parent 指向封存卡），接口绝不自动签发。封存卡与测量记录
保持只读；派生卡的 `correction` 块、版本谱系与 JSON 输出列明采用样本、补偿前后
角度（过压角、冲头闭合夹角、折叠角）与全部拒绝依据。

## 几何模型

`app/geometry.py` 多边形裁剪/耳切三角化/刚体变换；`app/partmodel.py` 将平板按所有
折弯线切成常号面，每次折叠把翻边侧的面绕折弯轴当前 3D 位置旋转，已成形翻边随动；
截面由折叠面的边界边与垂直于折弯轴的平面求交得到，因此完整覆盖翻边高度，且内在成形
结果与折弯顺序无关。`app/machine.py` 把工件放到机床侧视图（s 朝后挡料/机架、z 朝
滑块），建模凹模双肩块、V 开口、对称/非对称鹅颈冲头 relief 腔与机架墙，并对滑块
下行的中间角度做摆动采样。`app/planner.py` 做 DFS（按 id 定序、集合记忆化），
结果稳定可复现。

## 示例与测试

```bash
examples/feasible/u_channel.json                  # 可行件
examples/collision/deep_tray_small_press.json     # 机架/后挡料碰撞件
examples/collision/stepped_profile_no_order.json  # 任意顺序均失败件

python -m pytest -q
```
