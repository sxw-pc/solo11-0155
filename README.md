# 预制构件蒸养采证台

虚构预制件离线采录：模具与配比浇筑、带校准与时间间隔的蒸养曲线、同养试块强度及发运追责。三账分别落库，结果互相制约；迟到低强度复检保留旧放行并标记未发运冻结或已装车追溯。

低强度结果只证明该构件未达放行条件，**不能**直接断定原因在模具还是配比。`steamcast/investigation.py` 因此把每条低强度结果登记为一宗离线质量调查，沿模具与配比两条独立原因轴计算待核浇筑范围，并与 `cast`、`steam`、`strength` 的既有决定链、出库合同与追溯记录联动。

```powershell
python -m pip install . --no-build-isolation --no-deps
python -m steamcast demo --db demo.sqlite
python -m steamcast audit --db demo.sqlite
python -m unittest discover -s tests -v
```

## 三轴采录入口

独立入口使用 `--db state.sqlite --input fixtures/<file>.json`：

| 入口 | 证据 | `_bad.json` 展示 |
| --- | --- | --- |
| `cast-import` | 模具、配比审批、浇筑 | 模具不适配 |
| `steam-import` | 传感器证书、周期、温度点 | 校准失效 |
| `strength-import` | 实验室证书、试块、装车 | 强度不足 |
| `investigation-import` | 调查、模具检验、留样、独立复验、修复/换料、撤销 | — |
| `outbound-import` | 接收合同与出库请求 | — |

`investigation-import` 接受：`investigations`（保留原始试块、浇筑、周期、实验室证书与知悉时间）、`mould_checks`、`mix_samples`、`rechecks`（模具/配比独立复验）、`repairs`（修复或换料，仅截止未来暴露）、`revocations`（证书撤销）。同一文件重传幂等；同 ID 不同事实按冲突拒绝，不覆盖旧证据。

## 范围规则

- **时间裁定**：命中与否按浇筑**发生时间**判定；来源何时补录只改变当前已知结论，不伪造历史上已知。`--as-of` 只纳入该时刻之前已记录的检验/复验/修复。
- **待核集合**：调查未定因时，模具轴（自最后一次合格检验至有效修复）与配比轴（自最后一次合格留样至有效换料）的浇筑取并集。
- **单轴排除**：独立复验排除一条轴时只撤掉该轴独有的波及对象；两轴都未排除取并集；证据矛盾维持待核并列出冲突。
- **不自动解冻**：某件后来的合格试块不会替别的构件解除嫌疑；修复/换料只截止未来暴露，不抹掉已暴露件的历史风险。

## 出库用途

同一物理件在不同用途下可出库性不同，由事先签认的接收合同编号裁定：

- `engineering`：波及轴须均有排除或纠正证据。调查未定因时，即使试块合格也不得商品出库。
- `destructive_review`：可作为待核样本封签转运，回执仅用于实验室接收，库存仍为待核。

回执是历史事实：迟到复验、证书撤销和新低强度结果会追加新版本回执并留痕，绝不倒改旧合同与旧回执。已装车受影响件生成追溯记录，未装车件阻断发运。

```powershell
python -m steamcast investigations --db state.sqlite   # 模具轴/配比轴/并集与每件命中时间窗口
python -m steamcast receipts --db state.sqlite          # 每份回执的全部版次
python -m steamcast traces --db state.sqlite            # 追溯与阻断记录
python -m steamcast recompute --db state.sqlite         # 迟到证据后重算并追加新版回执
```

仅离线合成数据，不接真实蒸养设备。验收用三件合成构件见 `fixtures/abc_*.json` 与 `fixtures/investigation_ok.json`，测试见 `tests/test_investigation.py`。
