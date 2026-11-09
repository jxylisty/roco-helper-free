# 免费版 Python 运行环境

免费版使用 RKPP，不安装 OpenCV、NumPy、RapidOCR、ONNX Runtime 或 Cython。付费版的依赖清单不变。

1. 用 `tools/sync_free.py --main <主仓> --out <新候选目录>` 生成免费版。
2. 下载与构建机 Python 主次版本和架构一致的官方 Windows embeddable ZIP 或 Python NuGet 包。
3. 运行 `python tools/build_free_python.py --embed-zip <ZIP> --free-root <新候选目录>`（NuGet 使用 `--nuget <nupkg>`）。可用 `--wheelhouse <目录>` 离线安装依赖。脚本将运行环境放入 `python/`，并验证后端能在没有视觉库的情况下导入。
4. 编译候选目录的 Tauri exe，将它放在 `server_main.py` 同级。分发时带上 `python/` 和所需的 RKPP 解码器，不要复制开发机的 `.venv-build`、构建缓存或运行数据。

发包前在没有系统 Python 的机器上验证：启动、QQ 入群验证、窗口定位、实战 RKPP 识别、悬浮窗和 AI 军师。
