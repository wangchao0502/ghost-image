# Ghost Image

Ghost Image 是一个本地 Python 工具集，覆盖微博图片采集、人像导向的数据集处理、以及照片马赛克生成（CLI + Web）。

Language docs:
- English: `README.md`
- Chinese: `README.zh-CN.md`

## 功能特性
- 通过 CDP 连接已登录的浏览器会话。
- 采用类人工滚动节奏与低频请求强度。
- 按发布时间月份下载到 `images/YYYY-MM/`。
- 将元数据写入 `images/metadata.jsonl`，包含：
  - 微博文本
  - 发布时间
  - 帖子 URL
  - 图片 URL
  - 本地文件路径
- 在中断后可修复元数据/文件一致性。
- 支持人像筛选与居中裁剪，导出到 `datasets/...`。
- 支持本地 CLI 与本地 Web UI 两种马赛克生成方式。

## 项目结构
- `src/main.py` - 爬虫 CLI 入口
- `src/mosaic_cli.py` - 本地照片马赛克 CLI（主图 + 瓷砖图目录）
- `src/mosaic_web.py` - 本地 Web 版马赛克工具
- `src/mosaic_web/templates/` - Web UI 模板
- `src/mosaic_web/static/` - Web UI 静态资源
- `src/weibo_album_crawler/` - 爬虫模块
- `docs/specs/ghost-image/` - OpenSpec 文档

## 1) 准备浏览器（已登录）
使用启用远程调试的 Chrome/Edge。

示例 A（Windows Chrome）：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome-cdp"
```

示例 B（Windows Edge）：

```powershell
msedge.exe --remote-debugging-port=9222 --user-data-dir="C:\temp\edge-cdp"
```

然后在该浏览器窗口中手动登录微博。

可选检查：浏览器打开 `http://127.0.0.1:9222/json/version`，若返回 JSON 则 CDP 可用。

## 2) 创建虚拟环境
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## 3) 运行爬虫
仅采集元数据（不下载）：

```powershell
python src/main.py --dry-run --max-items 50
```

下载模式：

```powershell
python src/main.py
```

`python src/main.py` 是默认全量入口：
- 从主页 URL `https://weibo.com/u/1000000000` 开始
- 优先使用 API（`ajax/statuses/show`）提取文本、时间与原图列表
- 先写元数据再并发下载
- DOM 提取仅作为兜底（默认不做详情页回填，提升全量速度）

脱敏说明：
- 文档中的 `1000000000` / `1000000001` 和 `demo_blogger` 都是为提交安全做的示例占位值。
- 实际使用时请替换为你自己的主页 URL（例如 `https://weibo.com/u/<你的博主ID>`），或直接传入 `--blogger-id <你的博主ID>`。

如果浏览器使用了其他 CDP 端口，请显式指定：

```powershell
python src/main.py --cdp-url http://127.0.0.1:9333 --max-rounds 150
```

`src/main.py` 完整参数：
- `--cdp-url` 默认 `http://127.0.0.1:9222`
- `--album-url` 默认目标主页流（`https://weibo.com/u/1000000000`）
- `--blogger-id` 显式博主数字 ID（若可从 `--album-url` 解析可省略）
- `--blogger-name` 元数据显示名（默认 `demo_blogger`）
- `--dry-run` 仅采集元数据（不下载文件）
- `--max-items` 样本运行上限
- `--max-rounds` 全量滚动轮次上限（默认 `150`）
- `--stagnation-rounds` 连续无新增后停止（默认 `12`）
- `--download-concurrency` 并发下载数（默认 `3`）
- `--image-quality` 图片质量（默认 `large`，支持 `large`、`orj1080`、`orj360`、`mw690` 及其它 `orj*` / `mw*`）
- `--images-dir` 图片输出根目录（默认 `images`）
- `--log-file` 日志路径（默认 `<images-dir>/crawl.log`）

图片质量对比测试（同账号、每档 5 张、仅内存元数据）：

```powershell
python test/test_weibo_image_quality.py --album-url https://weibo.com/u/1000000001 --max-items 5 --qualities "large,mw690,orj360,orj1080"
```

`test/test_weibo_image_quality.py` 完整参数：
- `--cdp-url` 默认 `http://127.0.0.1:9222`
- `--album-url` 默认 `https://weibo.com/u/1000000001`
- `--blogger-id` 显式数字 ID（若可从 `--album-url` 解析可省略）
- `--blogger-name` 默认 `quality-test`
- `--max-items` 每种质量采集 URL 数（默认 `5`）
- `--qualities` 逗号分隔质量 token，默认 `"large,mw690,orj360,orj1080"`

## 4) 下载与清理流程

本节汇总下载与元数据/文件清理的推荐脚本和命令。

### 4.1 全量下载爬取（`src/main.py`）

推荐主入口：

```powershell
python src/main.py --album-url https://weibo.com/u/1000000000 --blogger-name "demo_blogger"
```

常用变体：

```powershell
# 样本运行
python src/main.py --max-items 50

# 仅元数据（不下载）
python src/main.py --dry-run --max-items 50

# 自定义输出目录
python src/main.py --images-dir images --log-file images/crawl.log
```

输出：
- 图片文件：`images/YYYY-MM/*.jpg`
- 元数据：`images/metadata.jsonl`
- 日志：`images/crawl.log`（或 `--log-file` 指定路径）

### 4.2 修复缺失文件 + 清理 `skipped_existing`（`src/repair_metadata.py`）

当元数据和本地文件可能不一致、或之前任务中断时使用。它会：
- 重新下载 `local_path` 丢失的记录
- 更新行级 `status/error` 字段
- 备份 `metadata.jsonl`
- 删除状态为 `skipped_existing` 的记录

```powershell
python src/repair_metadata.py --metadata images/metadata.jsonl --images-dir images
```

`src/repair_metadata.py` 完整参数：
- `--metadata` 元数据文件路径（默认 `images/metadata.jsonl`）
- `--images-dir` 重下载目标图片根目录（默认 `images`）
- `--backup-dir` 清理前元数据备份目录（默认 `images/backups`）
- `--log-file` 日志文件路径（默认 `images/repair_metadata.log`）
- `--request-timeout` 下载重试请求超时秒数（默认 `35.0`）

### 4.3 一致性检查

快速核对元数据与文件是否一致：

```powershell
python -c "import json; from pathlib import Path; m=Path(r'd:\projects\ghost-image\images\metadata.jsonl'); t=d=e=0; [ (lambda x: (globals().update(t=t+1), globals().update(d=d+1) if str(x.get('status') or '')=='downloaded' else None, globals().update(e=e+1) if str(x.get('status') or '')=='downloaded' and str(x.get('local_path') or '') and Path(str(x.get('local_path'))).exists() else None))(json.loads(s)) for s in m.read_text(encoding='utf-8').splitlines() if s.strip() ]; print('metadata_total=',t); print('downloaded_rows=',d); print('downloaded_files_exist=',e)"
```

如果 `downloaded_rows != downloaded_files_exist`，说明元数据仍有失效项或本地文件缺失。

## 5) 人像筛选 + 居中方形裁剪

这个离线步骤读取 `images/metadata.jsonl`，保留本地文件存在的记录，然后：
- 优先检测 `person`（支持全身与背影）
- 若无 person 再回退做人脸检测
- 做居中方形裁剪并导出 `300x300` jpg
- 单人图中：可见脸用 face-guided 框法（`face_guided_100_s100` 风格），背影/无脸用 upperbody-guided 框法（`upperbody_hint_s105` 风格）

每次运行输出到：
- `datasets/<process_code>_<yyyyMMddHHmm>/images/`
- `datasets/<process_code>_<yyyyMMddHHmm>/results.jsonl`

安装依赖后运行：

```powershell
python src/portrait_filter_crop.py --metadata images/metadata.jsonl --process-code demo --sample-size 20 --device 0 --batch-size 16
```

`src/portrait_filter_crop.py` 完整参数：
- `--metadata` 输入元数据 jsonl 路径（默认 `images/metadata.jsonl`）
- `--process-code` 运行代码（用于输出目录命名，必填）
- `--datasets-root` 输出根目录（默认 `datasets`）
- `--sample-size` 处理图片数量（默认 `20`）
- `--sample-mode first|random` 采样方式（默认 `first`）
- `--seed` 随机采样种子（默认 `42`）
- `--device` YOLO 设备（如 `0`、`1`、`cpu`）
- `--batch-size` YOLO 批大小（默认 `16`）
- `--yolo-imgsz` YOLO 推理尺寸（默认 `960`）
- `--person-conf-thres` 人体检测置信阈值（默认 `0.25`）
- `--face-conf-thres` Haar 人脸检测尺度因子（默认 `1.1`）
- `--face-min-neighbors` Haar 人脸检测最小邻居数（默认 `5`）
- `--face-detector-backend auto|yunet|haar` 人脸检测后端（默认 `auto`，优先 YuNet）
- `--yunet-model-path` YuNet ONNX 路径（默认 `models/face_detection_yunet_2023mar.onnx`）
- `--yunet-score-thres` YuNet 置信阈值（默认 `0.6`）
- `--out-size` 输出图像边长像素（默认 `300`）
- `--person-scale` 无脸单人图裁剪缩放（默认 `1.05`）
- `--face-scale` 基于人脸框裁剪缩放（默认 `1.8`）
- `--person-upperbody-ratio` 无脸单人图上半身中心比例（默认 `0.22`）
- `--center-tolerance` 相对中心允许偏移阈值（默认 `0.08`）
- `--target-face-image` 目标身份参考图（默认 `avator.jpg`）
- `--face-identity-backend simple|sface` 身份匹配后端（默认 `simple`）
- `--sface-model-path` SFace ONNX 路径（默认 `models/face_recognition_sface_2021dec.onnx`）
- `--face-match-thres` 身份匹配保留阈值（典型 `simple=0.45`，`sface=0.15`；默认 `0.15`）
- `--face-embedding-size` `simple` 后端的人脸向量边长（默认 `32`）
- `--write-workers` 并行写图 worker 数（默认 `8`）

## 6) 本地 CLI 马赛克（推荐快速测试）

直接使用本地路径（无需 Web 上传）是最快的调参方式：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src/mosaic_cli.py `
  --main-image "d:\projects\ghost-image\datasets\run20_final_strategy_202604222316\images\20260421_150140_64bc9a53a1__person.jpg" `
  --tiles-dir "d:\projects\ghost-image\datasets\run20_final_strategy_202604222316\images" `
  --output "d:\projects\ghost-image\datasets\mosaic_cli\result.jpg" `
  --grid-cols 80 `
  --tile-size 20 `
  --overlay-percent 20
```

`src/mosaic_cli.py` 完整参数：
- `--main-image` 主图路径（必填）
- `--tiles-dir` 瓷砖图目录（必填）
- `--output` 输出图片路径（必填）
- `--grid-cols` 网格列数（默认 `80`，范围 `20-200`）
- `--tile-size` 小图块像素（默认 `20`，范围 `8-80`）
- `--overlay-percent` 主图叠加百分比（默认 `20`，范围 `0-80`）
- `--diversity-strength` 多样性强度（默认 `0.03`，范围 `0-0.3`）
- `--max-reuse` 单个 tile 的最大复用次数（默认 `3`，`0` 表示不限制）
- `--sharpen-amount` 锐化强度（默认 `0.35`，范围 `0-2`）
- `--max-tiles` 限制 tile 数（默认 `0`，表示全部）
- `--recursive` 递归读取子目录 tile

输出：
- 马赛克图保存到 `--output`

大图库（如 7000+ 图片）推荐基线：

```powershell
python src/mosaic_cli.py `
  --main-image "path\to\main.jpg" `
  --tiles-dir "datasets\full_202604222337\images" `
  --output "datasets\mosaic_cli\result_best.jpg" `
  --grid-cols 180 `
  --tile-size 24 `
  --overlay-percent 8 `
  --diversity-strength 0.05 `
  --max-reuse 3 `
  --sharpen-amount 0.45
```

调参建议：
- 如果只用了很小一部分 tile，提高 `--diversity-strength` 到 `0.06-0.10`
- 如果颜色准确性下降，将 `--diversity-strength` 回调到 `0.02-0.05`
- 如果结果偏糊，可把 `--overlay-percent` 降到 `5-10`，并把 `--sharpen-amount` 提到 `0.4-0.7`
- 如果细节不足，优先提高 `--grid-cols`，其次提高 `--tile-size`

## 7) 共享马赛克核心（CLI + Web）

CLI 与 Web 都复用 `src/mosaic_cli.py` 的核心函数：
- `build_mosaic(...)`：tile 匹配和渲染
- `normalize_mosaic_params(...)`：统一参数边界

这样可保证本地脚本测试与浏览器使用行为一致。

## 8) 马赛克 Web 应用（可选）

项目包含本地 Web 版马赛克工具（主图 + tile 图目录）：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src/mosaic_web.py
```

打开 [http://127.0.0.1:5000](http://127.0.0.1:5000)，然后：
- 上传 1 张主图
- 本地调试时优先填写本地 tile 目录（如 `datasets/full_202604222337/images`）避免大文件上传 `413`
- 若不填本地 tile 目录，再选择包含大量小图的文件夹
- 调整 `grid_cols`、`tile_size`、`overlay_percent`（参数范围与 CLI 相同）
- 点击 **Generate**

生成结果保存到 `datasets/mosaic_web_outputs/`。

`src/mosaic_web.py` 当前无额外 CLI 参数（直接 `python src/mosaic_web.py` 启动）。

## 安全说明
- 脚本只执行读取、滚动和图片下载。
- 不会执行关注/点赞/评论等写操作。
- 建议保持低频下载，避免多个爬虫实例并发运行。
