# 预制构件蒸养采证台

虚构预制件离线采录：模具与配比浇筑、带校准与时间间隔的蒸养曲线、同养试块强度及发运追责。三账分别落库，结果互相制约；迟到低强度复检保留旧放行并标记未发运冻结或已装车追溯。

```powershell
python -m pip install . --no-build-isolation --no-deps
python -m steamcast demo --db demo.sqlite
python -m steamcast audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `cast-import`、`steam-import`、`strength-import` 使用 `--db state.sqlite --input fixtures/<cast|steam|strength>_ok.json`；`_bad.json` 分别展示模具不适配、校准失效与强度不足。仅离线合成数据，不接真实蒸养设备。
