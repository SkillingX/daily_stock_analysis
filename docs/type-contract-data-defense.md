# 类型-契约-数据 三层防御体系实践指南

> 基于 `daily_stock_analysis` 项目的真实代码与 CI 实践

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                   请求入口 (API / CLI)                       │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: Pydantic v2          数据形状 + 简单数值约束       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ @field_validator  Field(..., gt=0)  BaseModel           ││
│  └─────────────────────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│  Layer 2: icontract            业务前置/后置条件             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ @require  @ensure  复杂公式 + 不变量                    ││
│  └─────────────────────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│  Layer 1: mypy / pyright       编译期类型检查               │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 类型注解  Annotated  Literal  strict mode               ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

## 各层职责边界

| 层级 | 工具 | 职责 | 运行时机 | 示例 |
|------|------|------|---------|------|
| L1 类型 | mypy/pyright | 类型正确性 | CI + IDE | `def f(x: int) -> str` |
| L2 契约 | icontract | 业务正确性 | 运行时测试 | `@require(lambda x: x > 0)` |
| L3 数据 | Pydantic | 数据完整性 | I/O 边界 | `Field(..., gt=0, pattern=...)` |

**核心原则**：每层只管自己该管的事，不越界。

---

## Layer 1: mypy / pyright — 类型安全网

### 配置 (`pyproject.toml`)

```toml
[tool.pyright]
include = ["src", "data_provider", "api", "bot"]
typeCheckingMode = "standard"
pythonVersion = "3.11"
reportMissingTypeStubs = false
reportUnknownParameterType = false

[tool.mypy]
python_version = "3.11"
strict = false
warn_return_any = true
plugins = ["pydantic.mypy"]

[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true
```

### 实践要点

**1. 核心模块开启严格检查**

```toml
# 对关键模块强制类型注解
[[tool.mypy.overrides]]
module = ["data_provider.akshare_fetcher", "src.storage"]
disallow_untyped_defs = true
check_untyped_defs = true
```

**2. 用 `Annotated` 增强类型表达力**

```python
from typing import Annotated
from pydantic import Field

# 比单纯 `str` 更清晰
user_id: Annotated[str, Field(..., pattern=r"^[a-f0-9]{32}$")]
price: Annotated[Decimal, Field(..., gt=Decimal("0"), decimal_places=8)]
```

**3. 用 `Literal` 限缩值域**

```python
from typing import Literal

side: Literal["BUY", "SELL"]  # 比 str 更精确
basis: Literal["rule", "llm"] = "rule"
```

### CI 集成

全模块 audit 与治理模块检查必须分别验证；前者通过不能替代 `.github/workflows/type-safety.yml` 中的 `mypy --follow-imports=silent -p ...` 模块清单。补足 ORM 类型时，沿现有 `Mapped` / `mapped_column` 入口逐字段修改，并显式保留原 SQL 类型、可空性、默认值和索引；不以类型修复引入数据库迁移。正常与预算耗尽等分支共享的工具结果使用同一精确类型契约，保证排序和消息回填仍保留工具身份。

SQLAlchemy 支持同一模型逐字段混用旧 `Column` 与 `mapped_column`，见[官方说明](https://docs.sqlalchemy.org/en/21/orm/declarative_tables.html#declarative-table-with-mapped-column)。本项目对应回归为 `tests/test_governed_type_contracts.py`；修改 ORM 声明时还应比较修复前后的表、索引和约束，不能仅以静态检查通过认定结构不变。

```yaml
# .github/workflows/type-safety.yml
- name: Pyright type check
  run: pyright src data_provider api bot

- name: Mypy type audit
  run: mypy src data_provider api bot
```

---

## Layer 2: icontract — 业务契约

### 何时用 icontract 而非 Pydantic

| 场景 | 用 Pydantic | 用 icontract |
|------|------------|-------------|
| 字段存在性、类型、范围 | ✅ | |
| 正则 pattern 校验 | ✅ | |
| 多字段交叉校验 | | ✅ |
| 公式正确性（后置条件） | | ✅ |
| 业务不变量 | | ✅ |
| 复杂计算的前置条件 | | ✅ |

### 实践示例：`data_provider/realtime_types.py`

```python
from icontract import require, ensure

@require(lambda default: default is None or isinstance(default, (int, float)),
         "default must be a numeric scalar or None")
@ensure(lambda result: result is None or isinstance(result, float),
        "result must be a float or None")
def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    """类型转换函数的契约：输入输出类型必须符合约定。"""
    ...
```

### 实践示例：`src/schemas/risk_check.py`

```python
@require(lambda req: req.leverage >= Decimal("1"), "杠杆必须 >= 1")
@require(lambda req: req.account_balance > Decimal("0"), "账户余额必须为正")
@ensure(
    lambda req, result: result.margin_required == req.notional / req.leverage,
    "保证金计算公式必须精确匹配",
)
@ensure(
    lambda req, result: result.margin_ratio == result.margin_required / req.account_balance,
    "保证金占用率计算必须正确",
)
def check_risk(req: RiskCheckRequest) -> RiskCheckResult:
    """业务逻辑：Pydantic 守 I/O，icontract 守公式。"""
    margin_required = req.notional / req.leverage
    margin_ratio = margin_required / req.account_balance
    ...
```

### 测试契约

```python
# tests/test_risk_check_contract.py
from icontract import ViolationError

def test_check_risk_passes_with_sufficient_margin():
    req = RiskCheckRequest(...)
    result = check_risk(req)
    assert result.passed
    assert result.margin_required == Decimal("500")

def test_leverage_must_be_at_least_1():
    """违反 @require 时应抛出 ViolationError。"""
    req = RiskCheckRequest(leverage=Decimal("0.5"), ...)
    with self.assertRaises(ViolationError):
        check_risk(req)
```

### CI 集成

```yaml
# .github/workflows/type-safety.yml
- name: Contract smoke test
  env:
    ICONTRACT_SLOW: "true"  # 启用慢速契约检查
  run: |
    python -m pytest tests/test_realtime_types_contract.py \
                     tests/test_risk_check_contract.py -v --tb=short
```

---

## Layer 3: Pydantic v2 — 数据守门员

### 核心配置

```python
from pydantic import BaseModel, ConfigDict

class RiskCheckRequest(BaseModel):
    model_config = ConfigDict(
        strict=True,        # 禁止隐式类型转换
        frozen=True,        # 不可变，防止意外修改
        validate_assignment=True,  # 赋值时也校验
    )
```

### 字段约束速查

```python
from pydantic import Field, field_validator
from typing import Annotated

# 字符串
symbol: Annotated[str, Field(..., min_length=1, max_length=20)]
user_id: Annotated[str, Field(..., pattern=r"^[a-f0-9]{32}$")]

# 数值
price: Annotated[Decimal, Field(..., gt=Decimal("0"), decimal_places=8)]
leverage: Annotated[Decimal, Field(default=Decimal("1"), gt=Decimal("0"), le=Decimal("100"))]

# 枚举
side: Literal["BUY", "SELL"] = Field(...)

# 自定义校验器
@field_validator("symbol")
@classmethod
def validate_symbol_format(cls, v: str) -> str:
    if "-" not in v:
        raise ValueError("交易对格式必须为 BASE-QUOTE")
    return v.upper()
```

### 嵌套模型

```python
class Scenario(BaseModel):
    type: Literal["optimistic", "neutral", "pessimistic"]
    probability: float = Field(..., ge=0, le=1)
    description: Optional[str] = None

class ValueScenarios(BaseModel):
    scenarios: List[Scenario] = Field(default_factory=list)
    catalysts: List[str] = Field(default_factory=list)
```

---

## 三层协作模式

### 模式：API 端点

```python
# api/v1/endpoints/policy_minesweeper.py

# Layer 3: Pydantic 定义请求/响应结构
class PolicyMinesweeperRequest(BaseModel):
    stock_code: Annotated[str, Field(..., pattern=r"^\d{6}$")]
    company_name: Annotated[str, Field(..., min_length=1)]

# Layer 1: 类型注解
def generate_stream(request: PolicyMinesweeperRequest) -> Generator[str, None, None]:
    ...

# Layer 2: 可选的业务契约（复杂逻辑时加）
@require(lambda req: len(req.stock_code) == 6)
def process_request(req: PolicyMinesweeperRequest) -> ...
```

### 模式：数据层函数

```python
# data_provider/base.py

# Layer 1 + 2: 类型 + 契约
@require(lambda stock_code: isinstance(stock_code, str) and len(stock_code.strip()) > 0,
         "stock_code must be a non-empty string")
@ensure(lambda result: isinstance(result, str) and len(result) > 0,
        "normalized code must be a non-empty string")
def normalize_stock_code(stock_code: str) -> str:
    ...
```

### 模式：Schema 层

```python
# src/schemas/risk_check.py

# 三层全用
class RiskCheckRequest(BaseModel):     # L3: 数据形状
    price: Annotated[Decimal, Field(..., gt=Decimal("0"))]

@require(lambda req: req.leverage >= Decimal("1"))  # L2: 业务前置
def check_risk(req: RiskCheckRequest) -> RiskCheckResult:  # L1: 类型签名
    ...
```

---

## CI 流水线

```yaml
# .github/workflows/type-safety.yml
name: Type-Safety-Contract-Gate

on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r .github/requirements-ci.txt

      # 三层检查依次执行
      - name: Pyright type check        # L1: 类型
        run: pyright src data_provider api bot

      - name: Mypy type audit           # L1: 类型
        run: mypy src data_provider api bot

      - name: Contract smoke test       # L2: 契约
        env:
          ICONTRACT_SLOW: "true"
        run: |
          python -m pytest tests/test_realtime_types_contract.py \
                           tests/test_risk_check_contract.py -v

      # L3 Pydantic 由单元测试覆盖，无需单独步骤
```

---

## 选择指南

### 何时只用 Pydantic

- 纯 CRUD API，无复杂业务逻辑
- 数据来自外部（用户输入、消息队列）
- 只需校验字段类型和范围

### 何时加 icontract

- 函数有复杂的业务公式
- 多个输入参数之间有依赖关系
- 需要保证计算结果的正确性
- 金融/风控/量化场景

### 何时需要三层全用

- API 入口 + 复杂业务逻辑 + 金融计算
- 例：风控检查、保证金计算、仓位管理

---

## 常见陷阱

### 1. 重复校验

```python
# ❌ Pydantic 和 icontract 都检查同一件事
class Request(BaseModel):
    price: Annotated[Decimal, Field(..., gt=Decimal("0"))]

@require(lambda req: req.price > Decimal("0"))  # 冗余！
def process(req: Request): ...

# ✅ 各管各的
class Request(BaseModel):
    price: Annotated[Decimal, Field(..., gt=Decimal("0"))]  # Pydantic 管范围

@require(lambda req: req.price * req.quantity < req.balance)  # icontract 管交叉逻辑
def process(req: Request): ...
```

### 2. 契约的性能影响

```python
# 生产环境可用 ICONTRACT_SLOW=0 关闭
import os
if os.getenv("ICONTRACT_SLOW") == "true":
    from icontract import require, ensure
else:
    # 空操作装饰器
    def require(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    ensure = require
```

### 3. Decimal vs float

```python
# ❌ 金融计算用 float
price: float = 0.1
quantity: float = 0.2
total = price * quantity  # 0.30000000000000004

# ✅ 用 Decimal
price: Decimal = Decimal("0.1")
quantity: Decimal = Decimal("0.2")
total = price * quantity  # Decimal("0.02")
```

---

## 快速上手清单

1. **I/O 边界**：所有 API 请求/响应用 Pydantic BaseModel
2. **核心函数**：加类型注解，CI 跑 pyright + mypy
3. **业务逻辑**：有公式/不变量的函数加 icontract 装饰器
4. **CI**：三步检查 — pyright → mypy → contract tests
5. **Decimal**：金融计算永远用 Decimal，不用 float
6. **strict=True**：Pydantic 模型开启 strict 模式
