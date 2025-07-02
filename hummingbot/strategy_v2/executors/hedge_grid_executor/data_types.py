from decimal import Decimal
from enum import Enum
from typing import Literal, Optional, Dict

from pydantic import BaseModel, ConfigDict

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executors import TrackedOrder


class GridExecutorConfig(ExecutorConfigBase):
    """
    网格执行器配置类，定义了网格交易策略所需的所有参数
    """
    type: Literal["grid_executor"] = "grid_executor"  # 执行器类型，固定为grid_executor
    # Boundaries 边界设置
    connector_name: str  # 连接器名称，如交易所名称
    trading_pair: str  # 交易对，如BTC-USDT
    start_price: Decimal  # 网格起始价格
    end_price: Decimal  # 网格结束价格
    limit_price: Decimal  # 限制价格，用于风险控制
    side: TradeType = TradeType.BUY  # 交易方向，默认为买入
    # Profiling 性能配置
    total_amount_quote: Decimal  # 总报价金额（如USDT总量）
    min_spread_between_orders: Decimal = Decimal("0.0005")  # 订单之间的最小价差，默认为0.0005
    min_order_amount_quote: Decimal = Decimal("5")  # 最小订单报价金额，默认为5
    # Execution 执行配置
    max_open_orders: int = 5  # 最大开放订单数，默认为5
    max_orders_per_batch: Optional[int] = None  # 每批最大订单数，默认为None
    order_frequency: int = 0  # 订单频率，默认为0
    activation_bounds: Optional[Decimal] = None  # 激活边界，默认为None
    safe_extra_spread: Decimal = Decimal("0.0001")  # 安全额外价差，默认为0.0001
    # Risk Management 风险管理
    triple_barrier_config: TripleBarrierConfig  # 三重障碍配置，用于风险管理
    leverage: int = 20  # 杠杆倍数，默认为20倍
    level_id: Optional[str] = None  # 级别ID，默认为None
    deduct_base_fees: bool = False  # 是否扣除基础费用，默认为False
    keep_position: bool = False  # 是否保持仓位，默认为False
    coerce_tp_to_step: bool = True  # 是否将止盈价格强制调整为步长，默认为True
    # Hedge Mode 对冲模式配置
    hedge_mode: bool = False  # 是否启用对冲模式，默认为False
    hedge_connector_name: Optional[str] = None  # 对冲网格使用的连接器名称
    primary_account: Optional[str] = None  # 主账号标识
    hedge_account: Optional[str] = None  # 对冲账号标识
    is_primary: bool = True  # 是否为主网格
    hedge_executor_id: Optional[str] = None  # 对冲执行器ID
    # 预计算网格参数
    n_levels: Optional[int] = None  # 预计算的网格数量
    quote_amount_per_level: Optional[Decimal] = None  # 预计算的每个网格报价金额

    def create_hedge_config(self) -> 'GridExecutorConfig':
        """
        创建对冲配置，用于创建对冲网格执行器
        
        :return: 基于当前配置的对冲网格配置
        """
        # 使用当前配置的属性
        config_dict = self.dict()
        
        # 修改相关属性为对冲配置
        config_dict["is_primary"] = False
        # 对冲网格交换方向（买入变卖出，卖出变买入）
        config_dict["side"] = TradeType.SELL if self.side == TradeType.BUY else TradeType.BUY
        
        # 创建并返回新配置
        return GridExecutorConfig(**config_dict)


class GridLevelStates(Enum):
    """
    网格级别状态枚举，表示网格中每个价格级别的当前状态
    """
    NOT_ACTIVE = "NOT_ACTIVE"  # 未激活
    OPEN_ORDER_PLACED = "OPEN_ORDER_PLACED"  # 已下开仓订单
    OPEN_ORDER_FILLED = "OPEN_ORDER_FILLED"  # 开仓订单已成交
    CLOSE_ORDER_PLACED = "CLOSE_ORDER_PLACED"  # 已下平仓订单
    COMPLETE = "COMPLETE"  # 完成（开仓和平仓都已完成）


class GridLevel(BaseModel):
    """
    网格级别类，表示网格中的一个价格级别
    """
    id: str  # 级别唯一标识符
    price: Decimal  # 此级别的价格
    amount_quote: Decimal  # 此级别的报价金额（如USDT金额）
    take_profit: Decimal  # 止盈比例或价格
    side: TradeType  # 交易方向（买入或卖出）
    open_order_type: OrderType  # 开仓订单类型
    take_profit_order_type: OrderType  # 止盈订单类型
    active_open_order: Optional[TrackedOrder] = None  # 当前活跃的开仓订单，默认为None
    active_close_order: Optional[TrackedOrder] = None  # 当前活跃的平仓订单，默认为None
    state: GridLevelStates = GridLevelStates.NOT_ACTIVE  # 级别状态，默认为未激活
    model_config = ConfigDict(arbitrary_types_allowed=True)  # 模型配置，允许任意类型

    def update_state(self):
        """
        更新当前网格级别的状态
        """
        if self.active_open_order is None:
            self.state = GridLevelStates.NOT_ACTIVE
        elif self.active_open_order.is_filled:
            self.state = GridLevelStates.OPEN_ORDER_FILLED
        else:
            self.state = GridLevelStates.OPEN_ORDER_PLACED
            
        if self.active_close_order is not None:
            if self.active_close_order.is_filled:
                self.state = GridLevelStates.COMPLETE
            else:
                self.state = GridLevelStates.CLOSE_ORDER_PLACED

    def reset_open_order(self):
        """
        重置开仓订单状态
        """
        self.active_open_order = None
        self.state = GridLevelStates.NOT_ACTIVE

    def reset_close_order(self):
        """
        重置平仓订单状态
        """
        self.active_close_order = None
        self.state = GridLevelStates.OPEN_ORDER_FILLED

    def reset_level(self):
        """
        完全重置网格级别状态
        """
        self.active_open_order = None
        self.active_close_order = None
        self.state = GridLevelStates.NOT_ACTIVE 