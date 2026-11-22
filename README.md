# 预制构件蒸养采证台

虚构预制件离线采录：模具与配比浇筑、带校准与时间间隔的蒸养曲线、同养试块强度及发运追责。三账分别落库，结果互相制约；迟到低强度复检保留旧放行并标记未发运冻结或已装车追溯。

```powershell
python -m pip install . --no-build-isolation --no-deps
python -m steamcast demo --db demo.sqlite
python -m steamcast audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `cast-import`、`steam-import`、`strength-import` 使用 `--db state.sqlite --input fixtures/<cast|steam|strength>_ok.json`；`_bad.json` 分别展示模具不适配、校准失效与强度不足。仅离线合成数据，不接真实蒸养设备。

## 模拟移件票（handling-import）

`handling-import --input fixtures/release_lifting_bench.json` 以该文件为模拟移件台：一维静力、每台三个候选吊点、两个标牌朝向、区间重心。试块合格（released）并不等于可移件——未采到移件证据时只签 `test_request`，列出能消除同一反例位置的后续观测（停台双称台反推重心区间并录方向花押，或换机械均衡宽框做双见证载荷包络试验）；证据确实覆盖某一固定配置才发 `ticket_issued`，票只承诺一对物理吊点与一种已见证台面朝向，并引用仍有效的放行版次与本件台上工位。称台证据只适用于固定两钩框；宽框包络不得转借旧窄框或换向工位。未领取的票在放行改版或原件改版导致不能核证时 `frozen`；已离台的票保持 `moved` 不被改写，`report` 输出带具体原件版本的事后核对，`audit` 重放可还原同一业务输出。
