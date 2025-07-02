import asyncio  # 导入异步IO库
import logging  # 导入日志记录模块
import math  # 导入数学计算模块
from decimal import Decimal  # 导入Decimal类用于精确的小数计算
from typing import Dict, List, Optional, Union  # 导入类型提示相关模块

from hummingbot.connector.connector_base import ConnectorBase  # 导入连接器基类
from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType, TradeType  # 导入常用数据类型
from hummingbot.core.data_type.order_candidate import OrderCandidate, PerpetualOrderCandidate  # 导入订单候选类
from hummingbot.core.event.events import (  # 导入事件相关类
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.logger import HummingbotLogger  # 导入日志记录器
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase  # 导入脚本策略基类
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase  # 导入执行器基类
from hummingbot.strategy_v2.executors.hedge_grid_executor.data_types import GridExecutorConfig, GridLevel, GridLevelStates  # 导入对冲网格执行器数据类型
from hummingbot.strategy_v2.models.base import RunnableStatus  # 导入可运行状态类
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder  # 导入执行器模型类
from hummingbot.strategy_v2.utils.distributions import Distributions  # 导入分布工具类


class HedgeGridExecutor(ExecutorBase):
    """
    对冲网格执行器类，实现对冲网格交易策略的核心逻辑
    """
    _logger = None  # 类级别的日志记录器

    @classmethod
    def logger(cls) -> HummingbotLogger:
        """
        获取类的日志记录器
        :return: 日志记录器实例
        """
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, strategy: ScriptStrategyBase, config: GridExecutorConfig,
                 update_interval: float = 1.0, max_retries: int = 10):
        """
        初始化网格执行器实例

        :param strategy: 执行器使用的策略
        :param config: 网格执行器配置，GridExecutorConfig类型
        :param update_interval: 执行器更新间隔，默认为1.0秒
        :param max_retries: 执行器最大重试次数，默认为10次
        """
        self.config: GridExecutorConfig = config  # 保存配置对象
        if config.triple_barrier_config.time_limit_order_type != OrderType.MARKET or \
                config.triple_barrier_config.stop_loss_order_type != OrderType.MARKET:
            error = "Only market orders are supported for time_limit and stop_loss"  # 只支持市价单作为时间限制和止损单类型
            self.logger().error(error)
            raise ValueError(error)
        
        # 添加对冲模式所需的连接器列表
        connectors = [config.connector_name]
        if config.hedge_mode and config.hedge_connector_name is not None:
            connectors.append(config.hedge_connector_name)
            
        super().__init__(strategy=strategy, config=config, connectors=connectors,
                         update_interval=update_interval)  # 调用父类初始化方法
        # 根据交易方向设置价格类型
        self.open_order_price_type = PriceType.BestBid if config.side == TradeType.BUY else PriceType.BestAsk  # 开仓订单价格类型
        self.close_order_price_type = PriceType.BestAsk if config.side == TradeType.BUY else PriceType.BestBid  # 平仓订单价格类型
        self.close_order_side = TradeType.BUY if config.side == TradeType.SELL else TradeType.SELL  # 平仓订单方向
        self.trading_rules = self.get_trading_rules(self.config.connector_name, self.config.trading_pair)  # 获取交易规则
        # 网格级别
        self.grid_levels = self._generate_grid_levels()  # 生成网格级别
        self.levels_by_state = {state: [] for state in GridLevelStates}  # 按状态分类的网格级别
        self._close_order: Optional[TrackedOrder] = None  # 平仓订单
        self._filled_orders = []  # 已成交订单列表
        self._failed_orders = []  # 失败订单列表
        self._canceled_orders = []  # 已取消订单列表

        # 初始化指标变量
        self.step = Decimal("0")  # 网格步长
        self.position_break_even_price = Decimal("0")  # 仓位盈亏平衡价格
        self.position_size_base = Decimal("0")  # 基础资产仓位大小
        self.position_size_quote = Decimal("0")  # 报价资产仓位大小
        self.position_fees_quote = Decimal("0")  # 报价资产仓位费用
        self.position_pnl_quote = Decimal("0")  # 报价资产仓位盈亏
        self.position_pnl_pct = Decimal("0")  # 仓位盈亏百分比
        self.open_liquidity_placed = Decimal("0")  # 已放置的开仓流动性
        self.close_liquidity_placed = Decimal("0")  # 已放置的平仓流动性
        self.realized_buy_size_quote = Decimal("0")  # 已实现的买入量（报价资产）
        self.realized_sell_size_quote = Decimal("0")  # 已实现的卖出量（报价资产）
        self.realized_imbalance_quote = Decimal("0")  # 已实现的不平衡量（报价资产）
        self.realized_fees_quote = Decimal("0")  # 已实现的费用（报价资产）
        self.realized_pnl_quote = Decimal("0")  # 已实现的盈亏（报价资产）
        self.realized_pnl_pct = Decimal("0")  # 已实现的盈亏百分比
        self.max_open_creation_timestamp = 0  # 最新开仓订单创建时间戳
        self.max_close_creation_timestamp = 0  # 最新平仓订单创建时间戳
        self._open_fee_in_base = False  # 开仓费用是否以基础资产计算

        # 追踪止损相关
        self._trailing_stop_trigger_pct: Optional[Decimal] = None  # 追踪止损触发百分比
        self._current_retries = 0  # 当前重试次数
        self._max_retries = max_retries  # 最大重试次数
        
        # 对冲模式相关
        self._hedge_executor = None  # 对冲执行器引用
        self._is_primary = config.is_primary  # 是否为主执行器
        self._hedge_sync_lock = asyncio.Lock()  # 对冲同步锁，防止操作冲突
        self._hedge_action_queue = asyncio.Queue()  # 对冲操作队列

    @property
    def is_perpetual(self) -> bool:
        """
        检查交易所连接器是否为永续合约类型

        :return: 如果是永续合约连接器则返回True，否则返回False
        """
        return self.is_perpetual_connector(self.config.connector_name)  # 调用父类方法判断是否为永续合约

    async def validate_sufficient_balance(self):
        """
        验证账户是否有足够的余额来开仓
        """
        mid_price = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)  # 获取中间价格
        total_amount_base = self.config.total_amount_quote / mid_price  # 计算基础资产总量
        if self.is_perpetual:  # 如果是永续合约
            order_candidate = PerpetualOrderCandidate(  # 创建永续订单候选
                trading_pair=self.config.trading_pair,
                is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
                order_type=self.config.triple_barrier_config.open_order_type,
                order_side=self.config.side,
                amount=total_amount_base,
                price=mid_price,
                leverage=Decimal(self.config.leverage),
            )
        else:  # 如果不是永续合约
            order_candidate = OrderCandidate(  # 创建普通订单候选
                trading_pair=self.config.trading_pair,
                is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
                order_type=self.config.triple_barrier_config.open_order_type,
                order_side=self.config.side,
                amount=total_amount_base,
                price=mid_price,
            )
        adjusted_order_candidates = self.adjust_order_candidates(self.config.connector_name, [order_candidate])  # 调整订单候选
        if adjusted_order_candidates[0].amount == Decimal("0"):  # 如果调整后的订单数量为0
            self.close_type = CloseType.INSUFFICIENT_BALANCE  # 设置关闭类型为余额不足
            self.logger().error("Not enough budget to open position.")  # 记录错误日志
            self.stop()  # 停止执行器

    def _generate_grid_levels(self):
        """
        生成网格级别列表，根据配置计算网格价格和数量
        :return: 网格级别列表
        """
        grid_levels = []  # 网格级别列表
        price = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)  # 获取当前中间价格
        
        # 如果控制器已经提供了预计算的网格数量和每格金额，则直接使用这些参数
        if self.config.n_levels is not None and self.config.quote_amount_per_level is not None:
            n_levels = self.config.n_levels
            quote_amount_per_level = self.config.quote_amount_per_level
            
            # 计算网格步长
            grid_range = (self.config.end_price - self.config.start_price) / self.config.start_price  # 网格价格范围（百分比）
            if n_levels > 1:
                self.step = grid_range / (n_levels - 1)  # 计算步长
                prices = Distributions.linear(n_levels, float(self.config.start_price), float(self.config.end_price))  # 生成线性分布的价格
            else:
                # 对于单一级别，使用范围的中点
                mid_price = (self.config.start_price + self.config.end_price) / 2  # 中间价格
                prices = [mid_price]  # 价格列表只有一个中间价格
                self.step = grid_range  # 步长为整个范围
            
            # 在对冲模式下，始终强制止盈等于网格步长
            if self.config.hedge_mode or self.config.coerce_tp_to_step:
                take_profit = self.step
            else:
                take_profit = max(self.step, self.config.triple_barrier_config.take_profit) if self.config.coerce_tp_to_step else self.config.triple_barrier_config.take_profit
                
            # 创建网格级别
            for i, price in enumerate(prices):  # 遍历所有价格
                grid_levels.append(  # 添加网格级别
                    GridLevel(
                        id=f"L{i}",  # 级别ID
                        price=price,  # 级别价格
                        amount_quote=quote_amount_per_level,  # 级别报价资产数量
                        take_profit=take_profit,  # 级别止盈设置
                        side=self.config.side,  # 级别交易方向
                        open_order_type=self.config.triple_barrier_config.open_order_type,  # 开仓订单类型
                        take_profit_order_type=self.config.triple_barrier_config.take_profit_order_type,  # 止盈订单类型
                    )
                )
                
            # 记录网格创建详情
            self.logger().info(
                f"HedgeGridExecutor {self.config.id} - Created {len(grid_levels)} grid levels with "  # 创建了多少个网格级别
                f"amount per level: {quote_amount_per_level:.4f} {self.config.trading_pair.split('-')[1]} "  # 每个级别的报价金额
                f"(base amount: {(quote_amount_per_level / price):.8f} {self.config.trading_pair.split('-')[0]}) "  # 每个级别的基础资产金额
                f"[USING PRECALCULATED PARAMETERS]"  # 使用预计算参数
            )
            return grid_levels  # 返回生成的网格级别列表
        
        # 如果没有提供预计算参数，则执行原有的动态计算逻辑
        # 从交易规则获取最小名义价值和基础资产增量
        min_notional = max(
            self.config.min_order_amount_quote,
            self.trading_rules.min_notional_size
        )  # 最小名义价值
        min_base_increment = self.trading_rules.min_base_amount_increment  # 基础资产最小增量
        # 为最小名义价值添加安全边际，以应对价格波动和量化误差
        min_notional_with_margin = min_notional * Decimal("1.05")  # 添加5%的安全边际
        # 计算满足最小名义价值和量化要求的最小基础资产数量
        min_base_amount = max(
            min_notional_with_margin / price,  # 根据名义价值要求的最小数量
            min_base_increment * Decimal(str(math.ceil(float(min_notional) / float(min_base_increment * price))))  # 根据增量要求的最小数量
        )
        # 量化最小基础资产数量
        min_base_amount = Decimal(
            str(math.ceil(float(min_base_amount) / float(min_base_increment)))) * min_base_increment
        # 验证量化后的数量满足最小名义价值要求
        min_quote_amount = min_base_amount * price  # 最小报价资产数量
        # 计算网格范围和最小步长
        grid_range = (self.config.end_price - self.config.start_price) / self.config.start_price  # 网格价格范围（百分比）
        min_step_size = max(
            self.config.min_spread_between_orders,  # 配置的最小订单间价差
            self.trading_rules.min_price_increment / price  # 交易规则的最小价格增量（按百分比）
        )
        # 根据总金额计算最大可能的级别数
        max_possible_levels = int(self.config.total_amount_quote / min_quote_amount)  # 最大可能级别数
        if max_possible_levels == 0:  # 如果无法创建一个级别
            # 如果连一个级别都无法创建，则创建一个具有最小金额的级别
            n_levels = 1  # 级别数为1
            quote_amount_per_level = min_quote_amount  # 每个级别的报价资产数量
        else:
            # 计算最佳级别数
            max_levels_by_step = int(grid_range / min_step_size)  # 根据步长计算的最大级别数
            n_levels = min(max_possible_levels, max_levels_by_step)  # 取两者的较小值
            # 计算每个级别的报价金额，确保满足量化后的最小值
            base_amount_per_level = max(
                min_base_amount,  # 最小基础资产数量
                Decimal(str(math.floor(float(self.config.total_amount_quote / (price * n_levels)) /
                               float(min_base_increment)))) * min_base_increment  # 根据总量和级别数计算的基础资产数量
            )
            quote_amount_per_level = base_amount_per_level * price  # 每个级别的报价资产数量
            # 如果总金额会被超过，调整级别数
            n_levels = min(n_levels, int(float(self.config.total_amount_quote) / float(quote_amount_per_level)))
        # 确保至少有一个级别
        n_levels = max(1, n_levels)
        # 生成均匀分布的价格级别
        if n_levels > 1:  # 如果有多个级别
            prices = Distributions.linear(n_levels, float(self.config.start_price), float(self.config.end_price))  # 生成线性分布的价格
            self.step = grid_range / (n_levels - 1)  # 计算步长
        else:  # 如果只有一个级别
            # 对于单一级别，使用范围的中点
            mid_price = (self.config.start_price + self.config.end_price) / 2  # 中间价格
            prices = [mid_price]  # 价格列表只有一个中间价格
            self.step = grid_range  # 步长为整个范围
        
        # 在对冲模式下，始终强制止盈等于网格步长
        if self.config.hedge_mode or self.config.coerce_tp_to_step:
            take_profit = self.step
        else:
            # 如果配置了coerce_tp_to_step，则使用网格步长作为止盈
            take_profit = max(self.step, self.config.triple_barrier_config.take_profit) if self.config.coerce_tp_to_step else self.config.triple_barrier_config.take_profit
            
        # 创建网格级别
        for i, price in enumerate(prices):  # 遍历所有价格
            grid_levels.append(  # 添加网格级别
                GridLevel(
                    id=f"L{i}",  # 级别ID
                    price=price,  # 级别价格
                    amount_quote=quote_amount_per_level,  # 级别报价资产数量
                    take_profit=take_profit,  # 级别止盈设置
                    side=self.config.side,  # 级别交易方向
                    open_order_type=self.config.triple_barrier_config.open_order_type,  # 开仓订单类型
                    take_profit_order_type=self.config.triple_barrier_config.take_profit_order_type,  # 止盈订单类型
                )
            )
        # 记录网格创建详情
        self.logger().info(
            f"HedgeGridExecutor {self.config.id} - Created {len(grid_levels)} grid levels with "  # 创建了多少个网格级别
            f"amount per level: {quote_amount_per_level:.4f} {self.config.trading_pair.split('-')[1]} "  # 每个级别的报价金额
            f"(base amount: {(quote_amount_per_level / price):.8f} {self.config.trading_pair.split('-')[0]})"  # 每个级别的基础资产金额
        )
        return grid_levels  # 返回生成的网格级别列表

    @property
    def end_time(self) -> Optional[float]:
        """
        根据时间限制计算仓位的结束时间。

        :return: 仓位的结束时间戳。
        """
        if not self.config.triple_barrier_config.time_limit:  # 如果没有设置时间限制
            return None  # 返回None
        return self.config.timestamp + self.config.triple_barrier_config.time_limit  # 返回开始时间戳加上时间限制

    @property
    def is_expired(self) -> bool:
        """
        检查仓位是否已到期。

        :return: 如果仓位已到期则返回True，否则返回False。
        """
        return self.end_time and self.end_time <= self._strategy.current_timestamp  # 检查是否存在结束时间且当前时间已超过结束时间

    @property
    def is_trading(self):
        """
        检查仓位是否正在交易中。

        :return: 如果正在交易则返回True，否则返回False。
        """
        return self.status == RunnableStatus.RUNNING and self.position_size_quote > Decimal("0")  # 检查状态是否为运行中且有持仓

    @property
    def is_active(self):
        """
        返回执行器是否处于活动状态（开启或交易中）。
        """
        return self._status in [RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED, RunnableStatus.SHUTTING_DOWN]  # 检查状态是否为运行中、未开始或正在关闭

    async def control_task(self):
        """
        此方法负责根据执行器的状态控制任务。

        :return: None
        """
        # 处理对冲操作
        if self.config.hedge_mode:
            await self.process_hedge_actions()
            
            # 添加双向健康检查，确保两个执行器状态同步
            if self._hedge_executor is not None:
                # 检查对冲执行器是否还在正常运行
                try:
                    # 检查对冲执行器状态
                    if self.status == RunnableStatus.RUNNING and self._hedge_executor._status != RunnableStatus.RUNNING:
                        # 如果对冲执行器已不在运行中，而当前执行器还在运行，则停止当前执行器
                        self.logger().warning(f"Hedge executor {self._hedge_executor.config.id} is not running "
                                            f"(status: {self._hedge_executor._status}). Stopping current executor.")
                        self.close_type = CloseType.HEDGE_STOPPED
                        self.cancel_open_orders()
                        self._status = RunnableStatus.SHUTTING_DOWN
                        return
                except Exception as e:
                    # 如果无法检查对冲执行器状态（可能因对方已被销毁），也应该停止当前执行器
                    self.logger().error(f"Error checking hedge executor status: {str(e)}. Stopping current executor.")
                    self.close_type = CloseType.HEDGE_ERROR
                    self.cancel_open_orders()
                    self._status = RunnableStatus.SHUTTING_DOWN
                    return
            
        self.update_grid_levels()  # 更新网格级别状态
        self.update_metrics()  # 更新各项指标
        if self.status == RunnableStatus.RUNNING:  # 如果执行器正在运行
            if self.control_triple_barrier():  # 检查是否触发了三重障碍（止盈、止损等）
                self.cancel_open_orders()  # 取消所有未成交订单
                self._status = RunnableStatus.SHUTTING_DOWN  # 将状态设置为正在关闭
                return  # 结束当前任务
            open_orders_to_create = self.get_open_orders_to_create()  # 获取要创建的开仓订单
            close_orders_to_create = self.get_close_orders_to_create()  # 获取要创建的平仓订单
            open_order_ids_to_cancel = self.get_open_order_ids_to_cancel()  # 获取要取消的开仓订单ID
            close_order_ids_to_cancel = self.get_close_order_ids_to_cancel()  # 获取要取消的平仓订单ID
            for level in open_orders_to_create:  # 遍历并创建所有待开仓的订单
                self.adjust_and_place_open_order(level)
                # 对冲同步：开仓订单
                if self.config.hedge_mode and self._is_primary:
                    await self.sync_hedge_order_placement(level, "open")
            for level in close_orders_to_create:  # 遍历并创建所有待平仓的订单
                self.adjust_and_place_close_order(level)
                # 对冲同步：平仓订单
                if self.config.hedge_mode and self._is_primary:
                    await self.sync_hedge_order_placement(level, "close")
            for orders_id_to_cancel in open_order_ids_to_cancel + close_order_ids_to_cancel:  # 遍历所有待取消的订单ID
                # TODO: 实现批量取消订单 (Implement batch order cancel)
                self._strategy.cancel(  # 调用策略的取消订单方法
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    order_id=orders_id_to_cancel
                )
                # 对冲同步：取消订单
                if self.config.hedge_mode and self._is_primary:
                    await self.sync_hedge_order_cancellation(orders_id_to_cancel)
        elif self.status == RunnableStatus.SHUTTING_DOWN:  # 如果执行器正在关闭
            await self.control_shutdown_process()  # 控制关闭过程
        self.evaluate_max_retries()  # 评估最大重试次数

    def early_stop(self, keep_position: bool = False):
        """
        此方法允许策略提前停止执行器。

        :return: None
        """
        self.cancel_open_orders()  # 取消所有未成交订单
        self._status = RunnableStatus.SHUTTING_DOWN  # 将状态设置为正在关闭
        self.close_type = CloseType.POSITION_HOLD if keep_position else CloseType.EARLY_STOP  # 根据参数决定是保留仓位还是提前停止

    def update_grid_levels(self):
        """
        更新所有网格级别的状态，并处理已完成的网格。
        """
        self.levels_by_state = {state: [] for state in GridLevelStates}  # 重置按状态分类的网格字典
        for level in self.grid_levels:  # 遍历所有网格级别
            level.update_state()  # 更新单个级别的状态
            self.levels_by_state[level.state].append(level)  # 将级别添加到对应状态的列表中
        completed = self.levels_by_state[GridLevelStates.COMPLETE]  # 获取所有已完成的级别
        # 获取已完成的订单并将其存入已成交订单列表
        for level in completed:  # 遍历已完成的级别
            if level.active_open_order.order.completely_filled_event.is_set() and level.active_close_order.order.completely_filled_event.is_set():  # 确认开仓和平仓订单都已完全成交
                open_order = level.active_open_order.order.to_json()  # 获取开仓订单信息
                close_order = level.active_close_order.order.to_json()  # 获取平仓订单信息
                self._filled_orders.append(open_order)  # 添加到已成交列表
                self._filled_orders.append(close_order)  # 添加到已成交列表
                self.levels_by_state[GridLevelStates.COMPLETE].remove(level)  # 从已完成列表中移除
                level.reset_level()  # 重置该级别状态，以便复用
                self.levels_by_state[GridLevelStates.NOT_ACTIVE].append(level)  # 将其移回未激活列表

    async def control_shutdown_process(self):
        """
        控制执行器的关闭过程，特别处理需要保留的仓位。
        """
        self.close_timestamp = self._strategy.current_timestamp  # 记录关闭时间戳
        open_orders_completed = self.open_liquidity_placed == Decimal("0")  # 检查所有开仓挂单是否已完成（无挂单）
        close_orders_completed = self.close_liquidity_placed == Decimal("0")  # 检查所有平仓挂单是否已完成（无挂单）

        if open_orders_completed and close_orders_completed:  # 如果所有挂单都已处理完毕
            if self.close_type == CloseType.POSITION_HOLD:  # 如果是保留仓位模式
                # 将已成交订单移至保留仓位列表，而不是常规的已成交列表
                for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_FILLED]:  # 遍历已开仓的级别
                    if level.active_open_order and level.active_open_order.order:
                        self._held_position_orders.append(level.active_open_order.order.to_json())  # 添加到保留仓位列表
                    level.reset_level()  # 重置级别
                for level in self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]:  # 遍历已挂平仓单的级别
                    if level.active_close_order and level.active_close_order.order:
                        self._held_position_orders.append(level.active_close_order.order.to_json())  # 添加到保留仓位列表
                    level.reset_level()  # 重置级别
                if len(self._held_position_orders) == 0:  # 如果没有仓位可以保留
                    self.close_type = CloseType.EARLY_STOP  # 将关闭类型改为提前停止
                self.levels_by_state = {}  # 清空状态字典
                self.stop()  # 停止执行器
            else:  # 对于非保留仓位的常规关闭流程
                order_execution_completed = self.position_size_base == Decimal("0")  # 检查仓位是否已完全平掉
                if order_execution_completed:  # 如果仓位已平
                    for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_FILLED]:  # 遍历已开仓的级别
                        if level.active_open_order and level.active_open_order.order:
                            self._filled_orders.append(level.active_open_order.order.to_json())  # 添加到已成交列表
                        level.reset_level()
                    for level in self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]:  # 遍历已挂平仓单的级别
                        if level.active_close_order and level.active_close_order.order:
                            self._filled_orders.append(level.active_close_order.order.to_json())  # 添加到已成交列表
                        level.reset_level()
                    if self._close_order and self._close_order.order:  # 如果有最终的平仓市价单
                        self._filled_orders.append(self._close_order.order.to_json())  # 添加到已成交列表
                        self._close_order = None
                    self.update_realized_pnl_metrics()  # 更新已实现盈亏指标
                    self.levels_by_state = {}  # 清空状态字典
                    self.stop()  # 停止执行器
                else:  # 如果仓位还未平掉
                    await self.control_close_order()  # 控制最终的平仓单
                    self._current_retries += 1  # 增加重试次数
        else:  # 如果还有挂单
            self.cancel_open_orders()  # 取消所有未成交的挂单
        await self._sleep(5.0)  # 等待5秒

    async def control_close_order(self):
        """
        此方法负责控制最终的平仓订单。
        如果平仓单已成交且开仓单已完成，则停止执行器。
        如果平仓单未下，则下达平仓单。
        如果平仓单未成交，则等待其成交并向连接器请求订单信息。
        """
        if self._close_order:  # 如果已存在平仓订单
            in_flight_order = self.get_in_flight_order(self.config.connector_name,
                                                       self._close_order.order_id) if not self._close_order.order else self._close_order.order  # 获取订单的最新状态
            if in_flight_order:  # 如果订单仍然存在
                self._close_order.order = in_flight_order  # 更新订单信息
                self.logger().info("Waiting for close order to be filled")  # 记录日志，等待成交
            else:  # 如果订单不存在（可能已失败）
                self._failed_orders.append(self._close_order.order_id)  # 将其ID添加到失败列表
                self._close_order = None  # 重置平仓订单
        elif not self.config.keep_position or self.close_type == CloseType.TAKE_PROFIT:  # 如果不是保留仓位模式，或者是因为止盈而关闭
            self.place_close_order_and_cancel_open_orders(close_type=self.close_type)  # 下达最终平仓单并取消所有挂单

    def adjust_and_place_open_order(self, level: GridLevel):
        """
        此方法负责调整并下达开仓订单。

        :param level: 需要调整和下单的网格级别。
        :return: None
        """
        order_candidate = self._get_open_order_candidate(level)  # 获取开仓订单候选
        self.adjust_order_candidates(self.config.connector_name, [order_candidate])  # 根据交易所规则调整订单
        if order_candidate.amount > 0:  # 如果调整后的数量大于0
            order_id = self.place_order(  # 下达订单
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                order_type=self.config.triple_barrier_config.open_order_type,
                amount=order_candidate.amount,
                price=order_candidate.price,
                side=order_candidate.order_side,
                position_action=PositionAction.OPEN,
            )
            level.active_open_order = TrackedOrder(order_id=order_id)  # 创建一个跟踪订单对象
            self.max_open_creation_timestamp = self._strategy.current_timestamp  # 更新最新开仓订单创建时间
            self.logger().debug(f"HedgeGridExecutor ID: {self.config.id} - Placing open order {order_id}")  # 记录调试日志

    def adjust_and_place_close_order(self, level: GridLevel):
        """
        调整并下达平仓（止盈）订单。
        """
        order_candidate = self._get_close_order_candidate(level)  # 获取平仓订单候选
        self.adjust_order_candidates(self.config.connector_name, [order_candidate])  # 调整订单
        if order_candidate.amount > 0:  # 如果数量大于0
            order_id = self.place_order(  # 下达订单
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                order_type=self.config.triple_barrier_config.take_profit_order_type,
                amount=order_candidate.amount,
                price=order_candidate.price,
                side=order_candidate.order_side,
                position_action=PositionAction.CLOSE,
            )
            level.active_close_order = TrackedOrder(order_id=order_id)  # 创建跟踪订单对象
            self.logger().debug(f"HedgeGridExecutor ID: {self.config.id} - Placing close order {order_id}")  # 记录调试日志

    def get_take_profit_price(self, level: GridLevel):
        """
        计算给定级别的止盈价格。
        """
        return level.price * (1 + level.take_profit) if self.config.side == TradeType.BUY else level.price * (1 - level.take_profit)  # 根据买卖方向计算止盈价

    def _get_open_order_candidate(self, level: GridLevel):
        """
        为给定的网格级别生成一个开仓订单候选对象。
        """
        if ((level.side == TradeType.BUY and level.price >= self.current_open_quote) or
                (level.side == TradeType.SELL and level.price <= self.current_open_quote)):  # 如果下单价格比当前市场价更差（滑点保护）
            entry_price = self.current_open_quote * (1 - self.config.safe_extra_spread) if level.side == TradeType.BUY else self.current_open_quote * (1 + self.config.safe_extra_spread)  # 使用带安全边际的当前市场价
        else:
            entry_price = level.price  # 否则使用网格价格
        if self.is_perpetual:  # 如果是永续合约
            return PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
                order_type=self.config.triple_barrier_config.open_order_type,
                order_side=self.config.side,
                amount=level.amount_quote / self.mid_price,
                price=entry_price,
                leverage=Decimal(self.config.leverage)
            )
        return OrderCandidate(  # 如果是现货
            trading_pair=self.config.trading_pair,
            is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
            order_type=self.config.triple_barrier_config.open_order_type,
            order_side=self.config.side,
            amount=level.amount_quote / self.mid_price,
            price=entry_price
        )

    def _get_close_order_candidate(self, level: GridLevel):
        """
        为给定的网格级别生成一个平仓订单候选对象。
        """
        take_profit_price = self.get_take_profit_price(level)  # 获取理论止盈价
        if ((level.side == TradeType.BUY and take_profit_price <= self.current_close_quote) or
                (level.side == TradeType.SELL and take_profit_price >= self.current_close_quote)):  # 如果止盈价比当前市场价更差
            take_profit_price = self.current_close_quote * (
                1 + self.config.safe_extra_spread) if level.side == TradeType.BUY else self.current_close_quote * (
                1 - self.config.safe_extra_spread)  # 使用带安全边际的市场价
        if level.active_open_order.fee_asset == self.config.trading_pair.split("-")[0] and self.config.deduct_base_fees:  # 如果手续费以基础资产支付且配置了扣除
            amount = level.active_open_order.executed_amount_base - level.active_open_order.cum_fees_base  # 从数量中扣除手续费
            self._open_fee_in_base = True  # 标记手续费在基础资产中
        else:
            amount = level.active_open_order.executed_amount_base  # 否则使用全部成交数量
        if self.is_perpetual:  # 如果是永续合约
            return PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.config.triple_barrier_config.take_profit_order_type.is_limit_type(),
                order_type=self.config.triple_barrier_config.take_profit_order_type,
                order_side=self.close_order_side,
                amount=amount,
                price=take_profit_price,
                leverage=Decimal(self.config.leverage)
            )
        return OrderCandidate(  # 如果是现货
            trading_pair=self.config.trading_pair,
            is_maker=self.config.triple_barrier_config.take_profit_order_type.is_limit_type(),
            order_type=self.config.triple_barrier_config.take_profit_order_type,
            order_side=self.close_order_side,
            amount=amount,
            price=take_profit_price
        )

    def update_metrics(self):
        """
        更新所有市场和仓位相关的指标。
        """
        self.mid_price = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)  # 更新中间价
        self.current_open_quote = self.get_price(self.config.connector_name, self.config.trading_pair,
                                                 price_type=self.open_order_price_type)  # 更新当前开仓报价
        self.current_close_quote = self.get_price(self.config.connector_name, self.config.trading_pair,
                                                  price_type=self.close_order_price_type)  # 更新当前平仓报价
        self.update_position_metrics()  # 更新仓位指标
        self.update_realized_pnl_metrics()  # 更新已实现盈亏指标

    def get_open_orders_to_create(self):
        """
        此方法负责控制开仓订单。它会检查每个网格级别是否存在开仓订单。
        如果没有，它将根据当前价格、最大开放订单数、每批最大订单数、激活边界和下单频率，
        从建议的网格级别中下达新订单。
        """
        n_open_orders = len(
            [level.active_open_order for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]])  # 计算当前挂单数量
        if (self.max_open_creation_timestamp > self._strategy.current_timestamp - self.config.order_frequency or
                n_open_orders >= self.config.max_open_orders):  # 如果下单过于频繁或挂单数已达上限
            return []  # 返回空列表，不下单
        levels_allowed = self._filter_levels_by_activation_bounds()  # 根据激活边界筛选允许的级别
        sorted_levels_by_proximity = self._sort_levels_by_proximity(levels_allowed)  # 按与当前价格的接近程度排序
        return sorted_levels_by_proximity[:self.config.max_orders_per_batch]  # 返回最接近的、符合批量的级别

    def get_close_orders_to_create(self):
        """
        此方法负责控制止盈。它将检查净盈亏百分比是否大于止盈百分比，并下达平仓订单。
        （注释与代码实现不完全匹配，代码实现是为所有已成交的开仓单创建止盈单）
        :return: None
        """
        close_orders_proposal = []  # 提议创建的平仓订单列表
        open_orders_filled = self.levels_by_state[GridLevelStates.OPEN_ORDER_FILLED]  # 获取所有开仓已成交的级别
        for level in open_orders_filled:  # 遍历这些级别
            if self.config.activation_bounds:  # 如果设置了激活边界
                tp_to_mid = abs(self.get_take_profit_price(level) - self.mid_price) / self.mid_price  # 计算止盈价与中间价的距离
                if tp_to_mid < self.config.activation_bounds:  # 如果距离在激活边界内
                    close_orders_proposal.append(level)  # 添加到提议列表
            else:  # 如果没有激活边界
                close_orders_proposal.append(level)  # 直接添加到提议列表
        return close_orders_proposal  # 返回提议列表

    def get_open_order_ids_to_cancel(self):
        """
        获取需要取消的开仓订单ID列表。
        """
        if self.config.activation_bounds:  # 如果设置了激活边界
            open_orders_to_cancel = []  # 待取消的开仓订单列表
            open_orders_placed = [level.active_open_order for level in
                                  self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]]  # 获取所有已挂单的开仓订单
            for order in open_orders_placed:  # 遍历这些订单
                price = order.price  # 获取订单价格
                if price:  # 如果价格存在
                    distance_pct = abs(price - self.mid_price) / self.mid_price  # 计算与中间价的距离
                    if distance_pct > self.config.activation_bounds:  # 如果距离超出激活边界
                        open_orders_to_cancel.append(order.order_id)  # 添加到取消列表
                        self.logger().debug(f"HedgeGridExecutor ID: {self.config.id} - Canceling open order {order.order_id}")  # 记录日志
            return open_orders_to_cancel  # 返回待取消列表
        return []  # 如果没有激活边界，返回空列表

    def get_close_order_ids_to_cancel(self):
        """
        此方法负责控制平仓订单。它将检查止盈价格是否大于当前价格并取消平仓订单。
        （注释与代码实现不完全匹配，代码逻辑是取消离中间价太远的平仓挂单）
        :return: None
        """
        if self.config.activation_bounds:  # 如果设置了激活边界
            close_orders_to_cancel = []  # 待取消的平仓订单列表
            close_orders_placed = [level.active_close_order for level in
                                   self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]]  # 获取所有已挂单的平仓订单
            for order in close_orders_placed:  # 遍历这些订单
                price = order.price  # 获取订单价格
                if price:  # 如果价格存在
                    distance_to_mid = abs(price - self.mid_price) / self.mid_price  # 计算与中间价的距离
                    if distance_to_mid > self.config.activation_bounds:  # 如果距离超出激活边界
                        close_orders_to_cancel.append(order.order_id)  # 添加到取消列表
            return close_orders_to_cancel  # 返回待取消列表
        return []  # 如果没有激活边界，返回空列表

    def _filter_levels_by_activation_bounds(self):
        """
        根据激活边界筛选未激活的网格级别。
        """
        not_active_levels = self.levels_by_state[GridLevelStates.NOT_ACTIVE]  # 获取所有未激活的级别
        if self.config.activation_bounds:  # 如果设置了激活边界
            if self.config.side == TradeType.BUY:  # 如果是买入方向
                activation_bounds_price = self.mid_price * (1 - self.config.activation_bounds)  # 计算激活下边界
                return [level for level in not_active_levels if level.price >= activation_bounds_price]  # 返回在边界内的级别
            else:  # 如果是卖出方向
                activation_bounds_price = self.mid_price * (1 + self.config.activation_bounds)  # 计算激活上边界
                return [level for level in not_active_levels if level.price <= activation_bounds_price]  # 返回在边界内的级别
        return not_active_levels  # 如果没有激活边界，返回所有未激活级别

    def _sort_levels_by_proximity(self, levels: List[GridLevel]):
        """
        按与中间价的接近程度对级别进行排序。
        """
        return sorted(levels, key=lambda level: abs(level.price - self.mid_price))  # 使用与中间价的绝对距离作为排序依据

    def control_triple_barrier(self):
        """
        此方法负责控制三重障碍。它控制止损、止盈、时间限制和追踪止损。

        :return: 如果触发任何一个障碍，返回True，否则返回False。
        """
        if self.stop_loss_condition():  # 检查止损条件
            self.close_type = CloseType.STOP_LOSS  # 设置关闭类型
            # 对冲同步：止损触发
            if self.config.hedge_mode and self._is_primary:
                asyncio.create_task(self.handle_hedge_triple_barrier(self.close_type))
            return True
        elif self.limit_price_condition():  # 检查价格限制条件
            self.close_type = CloseType.POSITION_HOLD if self.config.keep_position else CloseType.STOP_LOSS  # 设置关闭类型
            # 对冲同步：价格限制触发
            if self.config.hedge_mode and self._is_primary:
                asyncio.create_task(self.handle_hedge_triple_barrier(self.close_type))
            return True
        elif self.is_expired:  # 检查是否到期
            self.close_type = CloseType.TIME_LIMIT  # 设置关闭类型
            # 对冲同步：时间限制触发
            if self.config.hedge_mode and self._is_primary:
                asyncio.create_task(self.handle_hedge_triple_barrier(self.close_type))
            return True
        elif self.trailing_stop_condition():  # 检查追踪止损条件
            self.close_type = CloseType.TRAILING_STOP  # 设置关闭类型
            # 对冲同步：追踪止损触发
            if self.config.hedge_mode and self._is_primary:
                asyncio.create_task(self.handle_hedge_triple_barrier(self.close_type))
            return True
        elif self.take_profit_condition():  # 检查止盈条件
            self.close_type = CloseType.TAKE_PROFIT  # 设置关闭类型
            # 对冲同步：止盈触发
            if self.config.hedge_mode and self._is_primary:
                asyncio.create_task(self.handle_hedge_triple_barrier(self.close_type))
            return True
        return False  # 如果都未触发，返回False

    def take_profit_condition(self):
        """
        当中间价高于网格的结束价（买入网格）或低于开始价（卖出网格），
        并且没有活跃的执行器时，将触发止盈。
        （代码实现与注释不完全匹配，只检查价格条件）
        """
        if self.mid_price > self.config.end_price if self.config.side == TradeType.BUY else self.mid_price < self.config.start_price:  # 检查价格是否已走出网格范围
            return True
        return False

    def stop_loss_condition(self):
        """
        此方法负责控制止损。如果净盈亏百分比小于止损百分比，
        它将下达平仓订单并取消开仓订单。

        :return: None
        """
        if self.config.triple_barrier_config.stop_loss:  # 如果设置了止损
            return self.position_pnl_pct <= -self.config.triple_barrier_config.stop_loss  # 检查当前仓位盈亏是否达到止损阈值
        return False

    def limit_price_condition(self):
        """
        此方法负责控制价格限制。如果当前价格超过价格限制，
        它将下达平仓订单并取消开仓订单。

        :return: None
        """
        if self.config.limit_price:  # 如果设置了价格限制
            if self.config.side == TradeType.BUY:  # 如果是买入方向
                return self.mid_price <= self.config.limit_price  # 检查价格是否低于限制价格
            else:  # 如果是卖出方向
                return self.mid_price >= self.config.limit_price  # 检查价格是否高于限制价格
        return False

    def trailing_stop_condition(self):
        """
        控制追踪止损的逻辑。
        """
        if self.config.triple_barrier_config.trailing_stop:  # 如果设置了追踪止损
            net_pnl_pct = self.position_pnl_pct  # 获取当前仓位盈亏百分比
            if not self._trailing_stop_trigger_pct:  # 如果追踪止损尚未激活
                if net_pnl_pct > self.config.triple_barrier_config.trailing_stop.activation_price:  # 如果利润达到激活价格
                    self._trailing_stop_trigger_pct = net_pnl_pct - self.config.triple_barrier_config.trailing_stop.trailing_delta  # 设置初始的止损触发点
            else:  # 如果追踪止损已激活
                if net_pnl_pct < self._trailing_stop_trigger_pct:  # 如果当前利润回落到触发点以下
                    return True  # 触发追踪止损
                if net_pnl_pct - self.config.triple_barrier_config.trailing_stop.trailing_delta > self._trailing_stop_trigger_pct:  # 如果利润进一步增长
                    self._trailing_stop_trigger_pct = net_pnl_pct - self.config.triple_barrier_config.trailing_stop.trailing_delta  # 提升止损触发点
        return False

    def place_close_order_and_cancel_open_orders(self, close_type: CloseType, price: Decimal = Decimal("NaN")):
        """
        此方法负责下达最终平仓订单并取消所有挂单。
        如果开仓已成交量与平仓已成交量之差大于最小订单大小，则下达平仓订单。
        它还会取消所有挂单。

        :param close_type: 平仓订单的类型。
        :param price: 用于平仓订单的价格。
        :return: None
        """
        self.cancel_open_orders()  # 取消所有挂单
        
        # 对冲同步：在主执行器触发时，同步通知对冲执行器
        if self.config.hedge_mode and self._is_primary:
            asyncio.create_task(self.sync_with_hedge_executor("place_close_order", {
                "close_type": close_type,
                "price": price
            }))
            
        if self.position_size_base >= self.trading_rules.min_order_size:  # 如果当前持仓量大于最小订单大小
            order_id = self.place_order(  # 下达市价平仓单
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                order_type=OrderType.MARKET,
                amount=self.position_size_base,
                price=price,
                side=self.close_order_side,
                position_action=PositionAction.CLOSE,
            )
            self._close_order = TrackedOrder(order_id=order_id)  # 跟踪这个最终平仓单
            self.logger().debug(f"HedgeGridExecutor ID: {self.config.id} - Placing close order {order_id}")  # 记录日志
        self.close_type = close_type  # 设置关闭类型
        self._status = RunnableStatus.SHUTTING_DOWN  # 将状态设置为正在关闭

    def cancel_open_orders(self):
        """
        此方法负责取消所有挂单。

        :return: None
        """
        open_order_placed = [level.active_open_order for level in
                             self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]]  # 获取所有开仓挂单
        close_order_placed = [level.active_close_order for level in
                              self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]]  # 获取所有平仓挂单
        for order in open_order_placed + close_order_placed:  # 遍历所有挂单
            # TODO: 实现批量取消订单 (Implement cancel batch orders)
            if order:  # 如果订单对象存在
                self._strategy.cancel(  # 调用策略的取消方法
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    order_id=order.order_id
                )
                self.logger().debug("Removing open order")  # 记录日志
                self.logger().debug(f"HedgeGridExecutor ID: {self.config.id} - Canceling open order {order.order_id}")  # 记录日志

    def get_custom_info(self) -> Dict:
        """
        获取自定义信息，用于状态报告。
        """
        held_position_value = sum([
            Decimal(order["executed_amount_quote"])
            for order in self._held_position_orders
        ])  # 计算保留仓位的总价值

        return {
            "levels_by_state": {key.name: value for key, value in self.levels_by_state.items()},  # 按状态分类的级别信息
            "filled_orders": self._filled_orders,  # 已成交订单列表
            "held_position_orders": self._held_position_orders,  # 保留的仓位订单列表
            "held_position_value": held_position_value,  # 保留仓位价值
            "failed_orders": self._failed_orders,  # 失败订单列表
            "canceled_orders": self._canceled_orders,  # 已取消订单列表
            "realized_buy_size_quote": self.realized_buy_size_quote,  # 已实现买入额
            "realized_sell_size_quote": self.realized_sell_size_quote,  # 已实现卖出额
            "realized_imbalance_quote": self.realized_imbalance_quote,  # 已实现不平衡额
            "realized_fees_quote": self.realized_fees_quote,  # 已实现费用
            "realized_pnl_quote": self.realized_pnl_quote,  # 已实现盈亏（报价资产）
            "realized_pnl_pct": self.realized_pnl_pct,  # 已实现盈亏百分比
            "position_size_quote": self.position_size_quote,  # 当前仓位大小（报价资产）
            "position_fees_quote": self.position_fees_quote,  # 当前仓位费用
            "break_even_price": self.position_break_even_price,  # 盈亏平衡价
            "position_pnl_quote": self.position_pnl_quote,  # 当前仓位盈亏（未实现）
            "open_liquidity_placed": self.open_liquidity_placed,  # 挂出的开仓流动性
            "close_liquidity_placed": self.close_liquidity_placed,  # 挂出的平仓流动性
        }

    async def on_start(self):
        """
        此方法负责启动执行器并验证仓位是否已到期。
        基类方法会验证是否有足够的余额来下开仓订单。

        :return: None
        """
        await super().on_start()  # 调用父类的 on_start 方法
        self.update_metrics()  # 更新指标
        if self.control_triple_barrier():  # 启动时检查是否已触发关闭条件
            self.logger().error(f"Grid is already expired by {self.close_type}.")  # 如果是，则记录错误
            self._status = RunnableStatus.SHUTTING_DOWN  # 将状态设置为正在关闭

    def evaluate_max_retries(self):
        """
        此方法负责评估下单的最大重试次数，
        如果达到最大重试次数，则停止执行器。

        :return: None
        """
        if self._current_retries > self._max_retries:  # 如果当前重试次数超过最大值
            self.close_type = CloseType.FAILED  # 将关闭类型设置为失败
            self.stop()  # 停止执行器

    def update_tracked_orders_with_order_id(self, order_id: str):
        """
        此方法负责使用 InFlightOrder 的信息更新跟踪的订单，
        使用 order_id 作为引用。

        :param order_id: 用作引用的 order_id。
        :return: None
        """
        self.update_grid_levels()  # 首先更新网格级别状态
        in_flight_order = self.get_in_flight_order(self.config.connector_name, order_id)  # 获取订单的最新信息
        if in_flight_order:  # 如果成功获取到信息
            for level in self.grid_levels:  # 遍历所有级别
                if level.active_open_order and level.active_open_order.order_id == order_id:  # 如果是该级别的开仓单
                    level.active_open_order.order = in_flight_order  # 更新订单对象
                if level.active_close_order and level.active_close_order.order_id == order_id:  # 如果是该级别的平仓单
                    level.active_close_order.order = in_flight_order  # 更新订单对象
            if self._close_order and self._close_order.order_id == order_id:  # 如果是最终的平仓单
                self._close_order.order = in_flight_order  # 更新订单对象

    def process_order_created_event(self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        """
        此方法负责处理订单创建事件。
        这里我们将使用 order_id 更新 TrackedOrder。
        """
        self.update_tracked_orders_with_order_id(event.order_id)  # 使用订单ID更新跟踪的订单

    def process_order_filled_event(self, _, market, event: OrderFilledEvent):
        """
        此方法负责处理订单成交事件。
        这里我们将更新 _total_executed_amount_backup 的值，
        以备 InFlightOrder 不可用时使用。
        （注释与代码实现不完全匹配，代码是更新跟踪的订单状态）
        """
        self.update_tracked_orders_with_order_id(event.order_id)  # 使用订单ID更新跟踪的订单

    def process_order_completed_event(self, _, market, event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        """
        此方法负责处理订单完成事件。
        这里我们将检查该ID是否是跟踪的订单之一并更新状态。
        """
        self.update_tracked_orders_with_order_id(event.order_id)  # 使用订单ID更新跟踪的订单

    def process_order_canceled_event(self, _, market: ConnectorBase, event: OrderCancelledEvent):
        """
        此方法负责处理订单取消事件。
        """
        self.update_grid_levels()  # 更新网格级别状态
        levels_open_order_placed = [level for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]]  # 获取所有开仓挂单的级别
        levels_close_order_placed = [level for level in self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]]  # 获取所有平仓挂单的级别
        for level in levels_open_order_placed:  # 遍历开仓挂单
            if event.order_id == level.active_open_order.order_id:  # 如果是该级别的订单被取消
                self._canceled_orders.append(level.active_open_order.order_id)  # 添加到已取消列表
                self.max_open_creation_timestamp = 0  # 重置开仓时间戳，允许立即下单
                level.reset_open_order()  # 重置级别的开仓订单信息
        for level in levels_close_order_placed:  # 遍历平仓挂单
            if event.order_id == level.active_close_order.order_id:  # 如果是该级别的订单被取消
                self._canceled_orders.append(level.active_close_order.order_id)  # 添加到已取消列表
                self.max_close_creation_timestamp = 0  # 重置平仓时间戳
                level.reset_close_order()  # 重置级别的平仓订单信息
        if self._close_order and event.order_id == self._close_order.order_id:  # 如果是最终的平仓单被取消
            self._canceled_orders.append(self._close_order.order_id)  # 添加到已取消列表
            self._close_order = None  # 重置最终平仓单

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        """
        此方法负责处理订单失败事件。
        这里我们将把 InFlightOrder 添加到失败订单列表。
        """
        self.update_grid_levels()  # 更新网格级别状态
        levels_open_order_placed = [level for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]]  # 获取所有开仓挂单的级别
        levels_close_order_placed = [level for level in self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]]  # 获取所有平仓挂单的级别
        for level in levels_open_order_placed:  # 遍历开仓挂单
            if event.order_id == level.active_open_order.order_id:  # 如果是该级别的订单失败
                self._failed_orders.append(level.active_open_order.order_id)  # 添加到失败列表
                self.max_open_creation_timestamp = 0  # 重置开仓时间戳
                level.reset_open_order()  # 重置级别的开仓订单信息
        for level in levels_close_order_placed:  # 遍历平仓挂单
            if event.order_id == level.active_close_order.order_id:  # 如果是该级别的订单失败
                self._failed_orders.append(level.active_close_order.order_id)  # 添加到失败列表
                self.max_close_creation_timestamp = 0  # 重置平仓时间戳
                level.reset_close_order()  # 重置级别的平仓订单信息
        if self._close_order and event.order_id == self._close_order.order_id:  # 如果是最终的平仓单失败
            self._failed_orders.append(self._close_order.order_id)  # 添加到失败列表
            self._close_order = None  # 重置最终平仓单

    def update_position_metrics(self):
        """
        计算报价资产中的未实现盈亏。

        :return: 报价资产中的未实现盈亏。
        """
        open_filled_levels = self.levels_by_state[GridLevelStates.OPEN_ORDER_FILLED] + self.levels_by_state[
            GridLevelStates.CLOSE_ORDER_PLACED]  # 获取所有已开仓但未平仓的级别
        side_multiplier = 1 if self.config.side == TradeType.BUY else -1  # 根据交易方向设置乘数
        executed_amount_base = Decimal(sum([level.active_open_order.order.amount for level in open_filled_levels]))  # 计算总的开仓基础资产数量
        if executed_amount_base == Decimal("0"):  # 如果没有持仓
            self.position_size_base = Decimal("0")  # 重置仓位大小
            self.position_size_quote = Decimal("0")  # 重置仓位价值
            self.position_fees_quote = Decimal("0")  # 重置仓位费用
            self.position_pnl_quote = Decimal("0")  # 重置未实现盈亏
            self.position_pnl_pct = Decimal("0")  # 重置未实现盈亏百分比
            self.close_liquidity_placed = Decimal("0")  # 重置平仓流动性
        else:  # 如果有持仓
            self.position_break_even_price = sum(
                [level.active_open_order.order.price * level.active_open_order.order.amount
                 for level in open_filled_levels]) / executed_amount_base  # 计算盈亏平衡价
            if self._open_fee_in_base:  # 如果手续费以基础资产计
                executed_amount_base -= sum([level.active_open_order.cum_fees_base for level in open_filled_levels])  # 从数量中扣除
            close_order_size_base = self._close_order.executed_amount_base if self._close_order and self._close_order.is_done else Decimal(
                "0")  # 获取最终平仓单的已成交数量
            self.position_size_base = executed_amount_base - close_order_size_base  # 计算当前净持仓量
            self.position_size_quote = self.position_size_base * self.position_break_even_price  # 计算当前持仓价值
            self.position_fees_quote = Decimal(sum([level.active_open_order.cum_fees_quote for level in open_filled_levels]))  # 计算总的开仓费用
            self.position_pnl_quote = side_multiplier * ((self.mid_price - self.position_break_even_price) / self.position_break_even_price) * self.position_size_quote - self.position_fees_quote  # 计算未实现盈亏
            self.position_pnl_pct = self.position_pnl_quote / self.position_size_quote if self.position_size_quote > 0 else Decimal(
                "0")  # 计算未实现盈亏百分比
            self.close_liquidity_placed = sum([level.amount_quote for level in self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED] if level.active_close_order and level.active_close_order.executed_amount_base == Decimal("0")])  # 计算已挂出的平仓流动性
        if len(self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]) > 0:  # 如果有开仓挂单
            self.open_liquidity_placed = sum([level.amount_quote for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED] if level.active_open_order and level.active_open_order.executed_amount_base == Decimal("0")])  # 计算已挂出的开仓流动性
        else:
            self.open_liquidity_placed = Decimal("0")  # 否则为0

    def update_realized_pnl_metrics(self):
        """
        计算报价资产中的已实现盈亏，不包括保留的仓位。
        """
        if len(self._filled_orders) == 0:  # 如果没有已成交的订单
            self._reset_metrics()  # 重置指标
            return
        # 仅计算完全关闭的交易（非保留仓位）的指标
        regular_filled_orders = [order for order in self._filled_orders
                                 if order not in self._held_position_orders]  # 筛选出非保留的已成交订单
        if len(regular_filled_orders) == 0:  # 如果没有此类订单
            self._reset_metrics()  # 重置指标
            return
        if self._open_fee_in_base:  # 如果手续费以基础资产计
            self.realized_buy_size_quote = sum([
                Decimal(order["executed_amount_quote"]) - Decimal(order["cumulative_fee_paid_quote"])
                for order in regular_filled_orders if order["trade_type"] == TradeType.BUY.name
            ])  # 计算已实现买入额（扣除费用）
        else:
            self.realized_buy_size_quote = sum([
                Decimal(order["executed_amount_quote"])
                for order in regular_filled_orders if order["trade_type"] == TradeType.BUY.name
            ])  # 计算已实现买入额
        self.realized_sell_size_quote = sum([
            Decimal(order["executed_amount_quote"])
            for order in regular_filled_orders if order["trade_type"] == TradeType.SELL.name
        ])  # 计算已实现卖出额
        self.realized_imbalance_quote = self.realized_buy_size_quote - self.realized_sell_size_quote  # 计算买卖不平衡额
        self.realized_fees_quote = sum([
            Decimal(order["cumulative_fee_paid_quote"])
            for order in regular_filled_orders
        ])  # 计算总的已实现费用
        self.realized_pnl_quote = (
            self.realized_sell_size_quote -
            self.realized_buy_size_quote -
            self.realized_fees_quote
        )  # 计算已实现盈亏
        self.realized_pnl_pct = (
            self.realized_pnl_quote / self.realized_buy_size_quote
            if self.realized_buy_size_quote > 0 else Decimal("0")
        )  # 计算已实现盈亏百分比

    def _reset_metrics(self):
        """
        重置所有盈亏指标的辅助方法。
        """
        self.realized_buy_size_quote = Decimal("0")
        self.realized_sell_size_quote = Decimal("0")
        self.realized_imbalance_quote = Decimal("0")
        self.realized_fees_quote = Decimal("0")
        self.realized_pnl_quote = Decimal("0")
        self.realized_pnl_pct = Decimal("0")

    def get_net_pnl_quote(self) -> Decimal:
        """
        计算报价资产中的净盈亏。

        :return: 报价资产中的净盈亏。
        """
        return self.position_pnl_quote + self.realized_pnl_quote if self.close_type != CloseType.POSITION_HOLD else self.realized_pnl_quote  # 返回 未实现盈亏 + 已实现盈亏

    def get_cum_fees_quote(self) -> Decimal:
        """
        计算报价资产中的累计费用。

        :return: 报价资产中的累计费用。
        """
        return self.position_fees_quote + self.realized_fees_quote if self.close_type != CloseType.POSITION_HOLD else self.realized_fees_quote  # 返回 当前仓位费用 + 已实现费用

    @property
    def filled_amount_quote(self) -> Decimal:
        """
        计算报价资产中的总成交额。

        :return: 报价资产中的总成交额。
        """
        matched_volume = self.realized_buy_size_quote + self.realized_sell_size_quote  # 计算已匹配的交易量
        return self.position_size_quote + matched_volume if self.close_type != CloseType.POSITION_HOLD else matched_volume  # 返回 当前持仓价值 + 已匹配交易量

    def get_net_pnl_pct(self) -> Decimal:
        """
        计算净盈亏百分比。

        :return: 净盈亏百分比。
        """
        return self.get_net_pnl_quote() / self.filled_amount_quote if self.filled_amount_quote > 0 else Decimal("0")  # 返回 净盈亏 / 总成交额

    # 对冲网格相关方法
    def set_hedge_executor(self, hedge_executor):
        """
        设置对冲执行器引用
        
        :param hedge_executor: 对冲网格执行器实例
        """
        self._hedge_executor = hedge_executor
        
    async def sync_with_hedge_executor(self, action_type, data=None):
        """
        与对冲执行器同步操作
        
        :param action_type: 操作类型
        :param data: 操作相关数据
        """
        if self.config.hedge_mode and self._hedge_executor is not None:
            # 如果是主执行器，则将操作放入对冲执行器的操作队列
            if self._is_primary:
                await self._hedge_executor._hedge_action_queue.put({
                    "type": action_type,
                    "data": data
                })
                self.logger().debug(f"Sent {action_type} action to hedge executor")
    
    async def process_hedge_actions(self):
        """
        处理来自主执行器的对冲操作
        """
        if self.config.hedge_mode and not self._is_primary:
            # 非主执行器处理队列中的操作
            try:
                while not self._hedge_action_queue.empty():
                    action = self._hedge_action_queue.get_nowait()
                    action_type = action.get("type")
                    data = action.get("data")
                    
                    if action_type == "cancel_open_orders":
                        self.cancel_open_orders()
                    elif action_type == "place_close_order":
                        close_type = data.get("close_type", CloseType.EARLY_STOP)
                        price = data.get("price", Decimal("NaN"))
                        self.place_close_order_and_cancel_open_orders(close_type, price)
                    elif action_type == "stop":
                        self.stop()
                        
                    self.logger().debug(f"Processed hedge action: {action_type}")
            except asyncio.QueueEmpty:
                pass
    
    async def sync_hedge_order_placement(self, level: GridLevel, order_type="open"):
        """
        同步对冲订单下单操作
        
        :param level: 下单的网格级别
        :param order_type: 订单类型，"open"表示开仓订单，"close"表示平仓订单
        """
        if self.config.hedge_mode and self._is_primary:
            await self.sync_with_hedge_executor("place_order", {
                "level_id": level.id,
                "order_type": order_type
            })
    
    async def sync_hedge_order_cancellation(self, order_id: str):
        """
        同步对冲订单取消操作
        
        :param order_id: 要取消的订单ID
        """
        if self.config.hedge_mode and self._is_primary:
            await self.sync_with_hedge_executor("cancel_order", {
                "order_id": order_id
            })
            
    async def handle_hedge_triple_barrier(self, close_type: CloseType):
        """
        处理三重障碍触发时的对冲同步
        
        :param close_type: 关闭类型
        """
        if self.config.hedge_mode and self._is_primary:
            await self.sync_with_hedge_executor("triple_barrier", {
                "close_type": close_type
            })

    async def _sleep(self, delay: float):
        """
        此方法负责让执行器休眠指定时间。

        :param delay: 休眠时间。
        :return: None
        """
        await asyncio.sleep(delay)  # 异步休眠