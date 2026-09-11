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
7. `GET /cards/{card_id}/lineage`、`GET /parts/{id}/cards` — 历史版本。

目录：`GET /catalog/{materials,dies,punches,presses}`（启动时内置 DC04/SUS304、
V12/V16/V24 凹模、R2 鹅颈/R3/R5 冲头、100t 与 50t 折弯机）。

## 每步输出

折弯线、工件朝向、进料方向、是否翻面、凹模/冲头、后挡料位置与接触边、预计吨位、
折弯/过压角度，以及全部核对项（`ok=true/false`）。无解时 `failure` 给出**最早失败
步骤**、该折弯，以及共同造成失败的约束代码集合（如 `min_flange`、`tonnage`、
`frame_collision`、`tool_collision`、`backgauge_reach`、`backgauge_occlusion`）。

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
