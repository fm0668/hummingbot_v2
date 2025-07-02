from decimal import Decimal  # 导入Decimal类用于精确的小数计算
import logging  # 导入日志模块
import math  # 导入数学模块
from typing import List, Optional, Tuple, Dict  # 导入类型提示相关模块

from pydantic import Field  # 导入Field用于模型字段定义

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType  # 导入常用数据类型
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig  # 导入蜡烛图配置类型
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase  # 导入控制器基类
from hummingbot.strategy_v2.executors.data_types import ConnectorPair  # 导入连接器对类型
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig  # 导入网格执行器配置
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig  # 导入三重障碍配置
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction  # 导入执行器动作
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo  # 导入执行器信息
from hummingbot.strategy_v2.utils.distributions import Distributions  # 导入分布工具类


class GridStrikeConfig(ControllerConfigBase):
    """
    Configuration required to run the GridStrike strategy for one connector and trading pair.
    运行GridStrike策略所需的配置，用于一个连接器和交易对。
    """
    controller_type: str = "generic"  # 控制器类型为通用型
    controller_name: str = "grid_strike"  # 控制器名称为grid_strike
    candles_config: List[CandlesConfig] = []  # 蜡烛图配置列表，默认为空

    # Account configuration 账户配置
    leverage: int = 20  # 杠杆倍数，默认为20倍
    position_mode: PositionMode = PositionMode.HEDGE  # 仓位模式，默认为对冲模式

    # Boundaries 边界设置
    connector_name: str = "binance_perpetual"  # 连接器名称，默认为币安永续
    trading_pair: str = "WLD-USDT"  # 交易对，默认为WLD-USDT
    side: TradeType = TradeType.BUY  # 交易方向，默认为买入
    start_price: Decimal = Field(default=Decimal("0.58"), json_schema_extra={"is_updatable": True})  # 开始价格，默认为0.58，可更新
    end_price: Decimal = Field(default=Decimal("0.95"), json_schema_extra={"is_updatable": True})  # 结束价格，默认为0.95，可更新
    limit_price: Decimal = Field(default=Decimal("0.55"), json_schema_extra={"is_updatable": True})  # 限制价格，默认为0.55，可更新

    # Profiling 性能配置
    total_amount_quote: Decimal = Field(default=Decimal("1000"), json_schema_extra={"is_updatable": True})  # 总报价金额，默认为1000，可更新
    min_spread_between_orders: Optional[Decimal] = Field(default=Decimal("0.001"), json_schema_extra={"is_updatable": True})  # 订单之间的最小价差，默认为0.001，可更新
    min_order_amount_quote: Optional[Decimal] = Field(default=Decimal("5"), json_schema_extra={"is_updatable": True})  # 最小订单报价金额，默认为5，可更新

    # Execution 执行配置
    max_open_orders: int = Field(default=2, json_schema_extra={"is_updatable": True})  # 最大开放订单数，默认为2，可更新
    max_orders_per_batch: Optional[int] = Field(default=1, json_schema_extra={"is_updatable": True})  # 每批最大订单数，默认为1，可更新
    order_frequency: int = Field(default=3, json_schema_extra={"is_updatable": True})  # 订单频率，默认为3，可更新
    activation_bounds: Optional[Decimal] = Field(default=None, json_schema_extra={"is_updatable": True})  # 激活边界，默认为None，可更新
    keep_position: bool = Field(default=False, json_schema_extra={"is_updatable": True})  # 是否保持仓位，默认为False，可更新

    # Risk Management 风险管理
    triple_barrier_config: TripleBarrierConfig = TripleBarrierConfig(
        take_profit=Decimal("0.001"),  # 止盈设置为0.001
        open_order_type=OrderType.LIMIT_MAKER,  # 开仓订单类型为限价做市商
        take_profit_order_type=OrderType.LIMIT_MAKER,  # 止盈订单类型为限价做市商
    )
    
    # Hedge Mode 对冲模式配置
    enable_hedge: bool = Field(default=False, json_schema_extra={"is_updatable": True})  # 是否启用对冲网格，默认为False，可更新
    hedge_connector_name: Optional[str] = Field(default=None, json_schema_extra={"is_updatable": True})  # 对冲网格的连接器名称（第二个币安账号）
    primary_account_api_key: Optional[str] = Field(default=None, json_schema_extra={"is_updatable": True})  # 主账号API密钥
    primary_account_api_secret: Optional[str] = Field(default=None, json_schema_extra={"is_updatable": True})  # 主账号API密钥
    hedge_account_api_key: Optional[str] = Field(default=None, json_schema_extra={"is_updatable": True})  # 对冲账号API密钥
    hedge_account_api_secret: Optional[str] = Field(default=None, json_schema_extra={"is_updatable": True})  # 对冲账号API密钥

    def update_markets(self, markets: MarketDict) -> MarketDict:
        """
        更新市场字典，添加或更新当前连接器和交易对
        """
        markets = markets.add_or_update(self.connector_name, self.trading_pair)
        # 添加对冲连接器市场（如果启用）
        if self.enable_hedge and self.hedge_connector_name:
            markets = markets.add_or_update(self.hedge_connector_name, self.trading_pair)
        return markets


class GridStrike(ControllerBase):
    def __init__(self, config: GridStrikeConfig, *args, **kwargs):
        """
        GridStrike控制器初始化
        :param config: GridStrike配置
        """
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_grid_levels_update = 0  # 上次网格级别更新时间
        self.trading_rules = None  # 交易规则
        self.grid_levels = []  # 网格级别列表
        self._primary_executor = None  # 主网格执行器
        self._hedge_executor = None  # 对冲网格执行器
        self.logger = logging.getLogger(__name__)  # 初始化日志器
        self.initialize_rate_sources()  # 初始化汇率源
        
    def precalculate_grid_parameters(self, connector_name: str, trading_pair: str, 
                                   start_price: Decimal, end_price: Decimal,
                                   total_amount_quote: Decimal, 
                                   min_spread_between_orders: Decimal,
                                   min_order_amount_quote: Decimal) -> Tuple[int, Decimal]:
        """
        预计算网格参数，确保多空网格使用完全一致的参数
        
        :param connector_name: 连接器名称
        :param trading_pair: 交易对
        :param start_price: 起始价格
        :param end_price: 结束价格
        :param total_amount_quote: 总报价金额
        :param min_spread_between_orders: 订单间最小价差
        :param min_order_amount_quote: 最小订单报价金额
        :return: 网格数量和每格报价金额的元组
        """
        try:
            # 获取交易规则
            connector = self._strategy.connectors.get(connector_name)
            if connector is None:
                self.logger.error(f"Connector {connector_name} not found")
                return 1, total_amount_quote  # 默认返回1个网格，所有资金
                
            trading_rule = connector.get_trading_rules().get(trading_pair)
            if trading_rule is None:
                self.logger.error(f"Trading rule for {trading_pair} not found")
                return 1, total_amount_quote  # 默认返回1个网格，所有资金
                
            price = self.market_data_provider.get_price_by_type(connector_name, trading_pair, PriceType.MidPrice)
            
            # 最小名义价值
            min_notional = max(min_order_amount_quote, trading_rule.min_notional_size)
            min_base_increment = trading_rule.min_base_amount_increment
            
            # 添加安全边际
            min_notional_with_margin = min_notional * Decimal("1.05")
            
            # 计算最小基础资产数量
            min_base_amount = max(
                min_notional_with_margin / price,
                min_base_increment * Decimal(str(math.ceil(float(min_notional) / float(min_base_increment * price))))
            )
            
            # 量化最小基础资产数量
            min_base_amount = Decimal(
                str(math.ceil(float(min_base_amount) / float(min_base_increment)))) * min_base_increment
                
            # 最小报价资产数量
            min_quote_amount = min_base_amount * price
            
            # 计算网格范围和最小步长
            grid_range = (end_price - start_price) / start_price
            min_step_size = max(
                min_spread_between_orders,
                trading_rule.min_price_increment / price
            )
            
            # 计算最大可能的级别数
            max_possible_levels = int(total_amount_quote / min_quote_amount)
            
            if max_possible_levels == 0:
                # 如果无法创建一个级别，则创建一个最小级别
                n_levels = 1
                quote_amount_per_level = min_quote_amount
            else:
                # 计算最佳级别数
                max_levels_by_step = int(grid_range / min_step_size)
                n_levels = min(max_possible_levels, max_levels_by_step)
                
                # 计算每个级别的报价金额
                base_amount_per_level = max(
                    min_base_amount,
                    Decimal(str(math.floor(float(total_amount_quote / (price * n_levels)) /
                                          float(min_base_increment)))) * min_base_increment
                )
                quote_amount_per_level = base_amount_per_level * price
                
                # 如果总金额会被超过，调整级别数
                n_levels = min(n_levels, int(float(total_amount_quote) / float(quote_amount_per_level)))
                
            # 确保至少有一个级别
            n_levels = max(1, n_levels)
            
            self.logger.info(
                f"Precalculated grid parameters: {n_levels} levels with "
                f"{quote_amount_per_level:.4f} {trading_pair.split('-')[1]} per level"
            )
            
            return n_levels, quote_amount_per_level
            
        except Exception as e:
            self.logger.error(f"Error precalculating grid parameters: {e}")
            return 1, total_amount_quote  # 默认返回1个网格，所有资金

    def initialize_rate_sources(self):
        """
        初始化汇率源
        """
        # 主网格的汇率源
        self.market_data_provider.initialize_rate_sources([ConnectorPair(connector_name=self.config.connector_name,
                                                                        trading_pair=self.config.trading_pair)])
        
        # 如果启用对冲网格，也初始化对冲网格的汇率源
        if self.config.enable_hedge and self.config.hedge_connector_name:
            self.market_data_provider.initialize_rate_sources([ConnectorPair(connector_name=self.config.hedge_connector_name,
                                                                            trading_pair=self.config.trading_pair)])

    def active_executors(self) -> List[ExecutorInfo]:
        """
        获取活跃的执行器列表
        :return: 活跃执行器列表
        """
        return [
            executor for executor in self.executors_info
            if executor.is_active
        ]

    def is_inside_bounds(self, price: Decimal) -> bool:
        """
        检查价格是否在设定的边界内
        :param price: 价格
        :return: 价格是否在边界内
        """
        return self.config.start_price <= price <= self.config.end_price

    def create_hedge_executor(self, primary_executor_id: str) -> ExecutorAction:
        """
        创建对冲执行器动作
        
        :param primary_executor_id: 主执行器ID
        :return: 创建执行器动作
        """
        # 创建主执行器配置的副本，并修改为对冲配置
        primary_config = GridExecutorConfig(
            timestamp=self.market_data_provider.time(),  # 当前时间戳
            connector_name=self.config.connector_name,  # 连接器名称
            trading_pair=self.config.trading_pair,  # 交易对
            start_price=self.config.start_price,  # 起始价格
            end_price=self.config.end_price,  # 结束价格
            leverage=self.config.leverage,  # 杠杆倍数
            limit_price=self.config.limit_price,  # 限制价格
            side=self.config.side,  # 交易方向
            total_amount_quote=self.config.total_amount_quote,  # 总报价金额
            min_spread_between_orders=self.config.min_spread_between_orders,  # 订单间最小价差
            min_order_amount_quote=self.config.min_order_amount_quote,  # 最小订单报价金额
            max_open_orders=self.config.max_open_orders,  # 最大开放订单数
            max_orders_per_batch=self.config.max_orders_per_batch,  # 每批最大订单数
            order_frequency=self.config.order_frequency,  # 订单频率
            activation_bounds=self.config.activation_bounds,  # 激活边界
            triple_barrier_config=self.config.triple_barrier_config,  # 三重障碍配置
            level_id=None,  # 级别ID为None
            keep_position=self.config.keep_position,  # 是否保持仓位
            coerce_tp_to_step=True,  # 强制将止盈与网格步长一致
            is_primary=True,  # 是主执行器
            hedge_mode=True,  # 启用对冲模式
            hedge_connector_name=self.config.hedge_connector_name,  # 对冲连接器名称
            primary_account=self.config.primary_account_api_key,  # 主账号标识
            hedge_account=self.config.hedge_account_api_key,  # 对冲账号标识
            n_levels=self._precalculated_n_levels,  # 使用预计算的网格数量 
            quote_amount_per_level=self._precalculated_quote_amount_per_level  # 使用预计算的每格金额
        )
        
        # 使用配置创建对冲配置
        hedge_config = primary_config.create_hedge_config()
        hedge_config.hedge_executor_id = primary_executor_id
        
        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=hedge_config
        )

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        确定执行器动作
        :return: 执行器动作列表
        """
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)  # 获取中间价格
            
        actions = []
        active_executors = self.active_executors()
        
        # 如果没有活跃执行器且价格在边界内，创建新的执行器
        if len(active_executors) == 0 and self.is_inside_bounds(mid_price):
            # 预计算网格参数，确保多空网格使用完全一致的参数
            self._precalculated_n_levels, self._precalculated_quote_amount_per_level = self.precalculate_grid_parameters(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                start_price=self.config.start_price,
                end_price=self.config.end_price,
                total_amount_quote=self.config.total_amount_quote,
                min_spread_between_orders=self.config.min_spread_between_orders,
                min_order_amount_quote=self.config.min_order_amount_quote
            )
            
            # 创建主网格执行器配置
            primary_config = GridExecutorConfig(
                timestamp=self.market_data_provider.time(),  # 当前时间戳
                connector_name=self.config.connector_name,  # 连接器名称
                trading_pair=self.config.trading_pair,  # 交易对
                start_price=self.config.start_price,  # 起始价格
                end_price=self.config.end_price,  # 结束价格
                leverage=self.config.leverage,  # 杠杆倍数
                limit_price=self.config.limit_price,  # 限制价格
                side=self.config.side,  # 交易方向
                total_amount_quote=self.config.total_amount_quote,  # 总报价金额
                min_spread_between_orders=self.config.min_spread_between_orders,  # 订单间最小价差
                min_order_amount_quote=self.config.min_order_amount_quote,  # 最小订单报价金额
                max_open_orders=self.config.max_open_orders,  # 最大开放订单数
                max_orders_per_batch=self.config.max_orders_per_batch,  # 每批最大订单数
                order_frequency=self.config.order_frequency,  # 订单频率
                activation_bounds=self.config.activation_bounds,  # 激活边界
                triple_barrier_config=self.config.triple_barrier_config,  # 三重障碍配置
                level_id=None,  # 级别ID为None
                keep_position=self.config.keep_position,  # 是否保持仓位
                coerce_tp_to_step=True,  # 强制将止盈与网格步长一致
                n_levels=self._precalculated_n_levels,  # 使用预计算的网格数量 
                quote_amount_per_level=self._precalculated_quote_amount_per_level  # 使用预计算的每格金额
            )
            
            # 如果启用对冲模式，设置相关配置
            if self.config.enable_hedge and self.config.hedge_connector_name:
                primary_config.hedge_mode = True
                primary_config.hedge_connector_name = self.config.hedge_connector_name
                primary_config.primary_account = self.config.primary_account_api_key
                primary_config.hedge_account = self.config.hedge_account_api_key
                primary_config.is_primary = True
            
            # 创建主网格执行器动作
            primary_action = CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=primary_config
            )
            actions.append(primary_action)
            
            # 如果启用对冲模式，创建对冲网格执行器动作
            # 这里我们需要主网格执行器的ID，但在创建时还没有
            # 我们将在on_executor_created方法中处理对冲网格的创建
            
        return actions
    
    def on_executor_created(self, executor_info: ExecutorInfo):
        """
        执行器创建后的回调
        
        :param executor_info: 执行器信息
        """
        super().on_executor_created(executor_info)
        
        # 如果是主网格执行器，且启用了对冲模式，创建对冲执行器
        config = executor_info.config
        if (self.config.enable_hedge and self.config.hedge_connector_name and 
            isinstance(config, GridExecutorConfig) and config.is_primary):
            # 保存主执行器引用
            self._primary_executor = executor_info.executor
            
            # 创建对冲执行器动作
            hedge_action = self.create_hedge_executor(executor_info.id)
            
            # 执行创建动作
            self._strategy.schedule_executor_action(hedge_action)
            
    def on_executor_updated(self, executor_info: ExecutorInfo):
        """
        执行器更新后的回调
        
        :param executor_info: 执行器信息
        """
        super().on_executor_updated(executor_info)
        
        # 如果是对冲执行器，设置主执行器和对冲执行器的相互引用
        config = executor_info.config
        if (isinstance(config, GridExecutorConfig) and config.hedge_mode and not config.is_primary and
            config.hedge_executor_id is not None):
            # 设置对冲执行器引用
            self._hedge_executor = executor_info.executor
            
            # 设置两个执行器的相互引用
            if self._primary_executor is not None and self._hedge_executor is not None:
                self._primary_executor.set_hedge_executor(self._hedge_executor)
                self._hedge_executor.set_hedge_executor(self._primary_executor)

    async def update_processed_data(self):
        """
        更新处理后的数据（此处为空实现）
        """
        pass

    def to_format_status(self) -> List[str]:
        """
        格式化状态信息
        :return: 状态信息字符串列表
        """
        status = []
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)  # 获取中间价格
            
        # 如果启用对冲模式，也获取对冲连接器的中间价格
        hedge_mid_price = None
        if self.config.enable_hedge and self.config.hedge_connector_name:
            try:
                hedge_mid_price = self.market_data_provider.get_price_by_type(
                    self.config.hedge_connector_name, self.config.trading_pair, PriceType.MidPrice)
            except:
                pass
                
        # 定义标准框宽度以保持一致性
        box_width = 114
        # 顶部网格配置框，使用简单边框
        status.append("┌" + "─" * box_width + "┐")  # 顶部边框
        # 第一行：网格配置和中间价格
        left_section = "Grid Configuration:"  # 网格配置标题
        padding = box_width - len(left_section) - 4  # -4表示边框字符和间距
        config_line1 = f"│ {left_section}{' ' * padding}"
        padding2 = box_width - len(config_line1) + 1  # +1用于正确对齐右边框
        config_line1 += " " * padding2 + "│"
        status.append(config_line1)
        # 第二行：配置参数
        config_line2 = f"│ Start: {self.config.start_price:.4f} │ End: {self.config.end_price:.4f} │ Side: {self.config.side} │ Limit: {self.config.limit_price:.4f} │ Mid Price: {mid_price:.4f} │"
        padding = box_width - len(config_line2) + 1  # +1用于正确对齐右边框
        config_line2 += " " * padding + "│"
        status.append(config_line2)
        # 第三行：最大订单数和是否在边界内
        config_line3 = f"│ Max Orders: {self.config.max_open_orders}   │ Inside bounds: {1 if self.is_inside_bounds(mid_price) else 0}"
        
        # 如果启用对冲模式，显示对冲信息
        if self.config.enable_hedge:
            hedge_info = f" │ Hedge Mode: ENABLED │ Hedge Price: {hedge_mid_price:.4f if hedge_mid_price else 'N/A'}"
            config_line3 += hedge_info
            
        padding = box_width - len(config_line3) + 1  # +1用于正确对齐右边框
        config_line3 += " " * padding + "│"
        status.append(config_line3)
        status.append("└" + "─" * box_width + "┘")  # 底部边框
        
        # 遍历活跃执行器，生成详细状态信息
        for level in self.active_executors():
            # 判断是否为对冲执行器
            is_hedge = False
            if isinstance(level.config, GridExecutorConfig):
                is_hedge = level.config.hedge_mode and not level.config.is_primary
                
            # 添加执行器类型标记到标题
            executor_type = " [HEDGE]" if is_hedge else " [PRIMARY]" if self.config.enable_hedge else ""
            
            # 定义列宽以进行完美对齐
            col_width = box_width // 3  # 将总宽度除以3，以获得相等的列
            total_width = box_width
            # 网格状态标题 - 使用长线和运行状态
            status_header = f"Grid Status: {level.id}{executor_type} (RunnableStatus.RUNNING)"
            status_line = f"┌ {status_header}" + "─" * (total_width - len(status_header) - 2) + "┐"
            status.append(status_line)
            # 计算精确的列宽以进行完美对齐
            col1_end = col_width
            # 列标题
            header_line = "│ Level Distribution" + " " * (col1_end - 20) + "│"  # 级别分布
            header_line += " Order Statistics" + " " * (col_width - 18) + "│"  # 订单统计
            header_line += " Performance Metrics" + " " * (col_width - 21) + "│"  # 性能指标
            status.append(header_line)
            # 三列的数据
            level_dist_data = [
                f"NOT_ACTIVE: {len(level.custom_info['levels_by_state'].get('NOT_ACTIVE', []))}",  # 未激活级别
                f"OPEN_ORDER_PLACED: {len(level.custom_info['levels_by_state'].get('OPEN_ORDER_PLACED', []))}",  # 已下开仓订单
                f"OPEN_ORDER_FILLED: {len(level.custom_info['levels_by_state'].get('OPEN_ORDER_FILLED', []))}",  # 开仓订单已成交
                f"CLOSE_ORDER_PLACED: {len(level.custom_info['levels_by_state'].get('CLOSE_ORDER_PLACED', []))}",  # 已下平仓订单
                f"COMPLETE: {len(level.custom_info['levels_by_state'].get('COMPLETE', []))}"  # 已完成
            ]
            order_stats_data = [
                f"Total: {sum(len(level.custom_info[k]) for k in ['filled_orders', 'failed_orders', 'canceled_orders'])}",  # 总订单数
                f"Filled: {len(level.custom_info['filled_orders'])}",  # 已成交订单
                f"Failed: {len(level.custom_info['failed_orders'])}",  # 失败订单
                f"Canceled: {len(level.custom_info['canceled_orders'])}"  # 已取消订单
            ]
            perf_metrics_data = [
                f"Buy Vol: {level.custom_info['realized_buy_size_quote']:.4f}",  # 已实现买入量
                f"Sell Vol: {level.custom_info['realized_sell_size_quote']:.4f}",  # 已实现卖出量
                f"R. PnL: {level.custom_info['realized_pnl_quote']:.4f}",  # 已实现盈亏
                f"R. Fees: {level.custom_info['realized_fees_quote']:.4f}",  # 已实现费用
                f"P. PnL: {level.custom_info['position_pnl_quote']:.4f}",  # 仓位盈亏
                f"Position: {level.custom_info['position_size_quote']:.4f}"  # 仓位大小
            ]
            # 构建完美对齐的行
            max_rows = max(len(level_dist_data), len(order_stats_data), len(perf_metrics_data))
            for i in range(max_rows):
                col1 = level_dist_data[i] if i < len(level_dist_data) else ""
                col2 = order_stats_data[i] if i < len(order_stats_data) else ""
                col3 = perf_metrics_data[i] if i < len(perf_metrics_data) else ""
                row = "│ " + col1
                row += " " * (col1_end - len(col1) - 2)  # -2表示开始处的"│ "
                row += "│ " + col2
                row += " " * (col_width - len(col2) - 2)  # -2表示col2前的"│ "
                row += "│ " + col3
                row += " " * (col_width - len(col3) - 2)  # -2表示col3前的"│ "
                row += "│"
                status.append(row)
            # 流动性行，完美对齐
            status.append("├" + "─" * total_width + "┤")
            liquidity_line = f"│ Open Liquidity: {level.custom_info['open_liquidity_placed']:.4f} │ Close Liquidity: {level.custom_info['close_liquidity_placed']:.4f} │"  # 开仓流动性和平仓流动性
            liquidity_line += " " * (total_width - len(liquidity_line) + 1)  # +1用于正确对齐右边框
            liquidity_line += "│"
            status.append(liquidity_line)
            status.append("└" + "─" * total_width + "┘")  # 底部边框
            
        # 如果启用对冲模式，添加对冲模式组合统计
        if self.config.enable_hedge and len(self.active_executors()) > 1:
            # 找出主执行器和对冲执行器
            primary_executor = None
            hedge_executor = None
            
            for level in self.active_executors():
                if isinstance(level.config, GridExecutorConfig):
                    if level.config.hedge_mode:
                        if level.config.is_primary:
                            primary_executor = level
                        else:
                            hedge_executor = level
            
            if primary_executor and hedge_executor:
                # 计算组合PnL
                try:
                    combined_pnl = (primary_executor.custom_info['position_pnl_quote'] + 
                                   hedge_executor.custom_info['position_pnl_quote'])
                    combined_realized_pnl = (primary_executor.custom_info['realized_pnl_quote'] + 
                                           hedge_executor.custom_info['realized_pnl_quote'])
                    total_position = (primary_executor.custom_info['position_size_quote'] + 
                                    hedge_executor.custom_info['position_size_quote'])
                    
                    # 添加组合统计表格
                    status.append("┌" + "─" * box_width + "┐")
                    status_header = "Combined Hedge Mode Statistics"
                    header_line = f"│ {status_header}" + " " * (box_width - len(status_header) - 4) + " │"
                    status.append(header_line)
                    
                    stats_line = f"│ Combined Position PnL: {combined_pnl:.4f} │ Combined Realized PnL: {combined_realized_pnl:.4f} │ Total Position: {total_position:.4f} │"
                    padding = box_width - len(stats_line) + 1
                    stats_line += " " * padding + "│"
                    status.append(stats_line)
                    status.append("└" + "─" * box_width + "┘")
                except:
                    # 如果计算失败，添加简单提示
                    status.append("┌" + "─" * box_width + "┐")
                    status.append("│ Hedge Mode Enabled - See individual grid statistics for details" + " " * (box_width - 65) + " │")
                    status.append("└" + "─" * box_width + "┘")
                
        return status
