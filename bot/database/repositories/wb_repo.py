from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Type
from sqlalchemy import Date, Numeric, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from bot.core.logging import db_logger

from bot.schemas.wb import NotifOrder, OrderWBCreate, SalesWBCreate, StockWBCreate
from bot.utils.utils import chunked_list
from ..models import OrdersWB, StocksWB, SalesWB
from ..repositories.base import SQLAlchemyRepository
from .base import T


class WBRepository(SQLAlchemyRepository[OrdersWB]):
    def __init__(self, session: AsyncSession, model: Type[T]):
        super().__init__(session, model)

    async def add_orders_bulk(self, orders: list[OrderWBCreate]) -> list[NotifOrder]:
        """Добавить заказы по одному для проверки."""
        db_logger.info("add_orders_bulk", count=len(orders))
        if not orders:
            return []

        new_orders = []
        for order in orders:
            data = order.model_dump()
            stmt = (
                insert(OrdersWB)
                .values(data)
                .on_conflict_do_nothing(
                    index_elements=['date', 'user_id', 'srid',
                                    'nm_id', 'is_cancel', 'tech_size']
                )
                .returning(OrdersWB)
            )
            try:
                result = await self.session.execute(stmt)
                inserted_order = result.scalar_one_or_none()
                if inserted_order:
                    new_orders.append(inserted_order)
            except SQLAlchemyError as e:
                db_logger.error("Error in add_orders_bulk", error=str(e))

        return [NotifOrder.model_validate(order) for order in new_orders]

    async def add_sales_bulk(self, orders: list[SalesWBCreate]) -> None:
        """Добавить продажи пачкой"""
        if not orders:
            return

        data = [order.model_dump() for order in orders]

        stmt = insert(SalesWB).values(data)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=['date', 'user_id',
                            'srid', 'nm_id', 'isCancel', 'tech_size']
        )
        await self.session.execute(stmt)

    async def add_stocks_bulk(self, stocks: list[StockWBCreate]) -> None:
        if not stocks:
            return

        CHUNK_SIZE = 500  # подбери значение экспериментально

        for chunk in chunked_list(stocks, CHUNK_SIZE):
            data = [stock.model_dump() for stock in chunk]

            stmt = insert(StocksWB).values(data)
            stmt = stmt.on_conflict_do_update(
                index_elements=['user_id', 'warehouse_name', 'nm_id'],
                set_=dict(
                    quantity=stmt.excluded.quantity,
                    last_change_date=stmt.excluded.last_change_date,
                )
            )
            await self.session.execute(stmt)

    async def counter_and_amount(self, user_id: int, order_id: int, date: datetime.date) -> int:
        """
        Возвращает номер заказа по порядку в рамках дня (счётчик),
        основываясь на id и дате (по полю OrdersWB.date).
        """
        try:
            stmt = select(
                func.count().label('order_count'),
                func.sum(
                    OrdersWB.total_price *
                        (1 - OrdersWB.discount_percent / 100)
                ).label("total_amount")
            ).where(
                OrdersWB.user_id == user_id,
                cast(OrdersWB.date, Date) == date,
                OrdersWB.id < order_id,
                OrdersWB.is_cancel.is_(False)
            )
            result = await self.session.execute(stmt)
            # или `result.first()`, если возможна пустая строка
            order_count, total_amount = result.one()
            total_amount = round(total_amount) if total_amount else 0
            return order_count + 1, total_amount

        except SQLAlchemyError as e:
            db_logger.error("Error in get_order_day_counter", error=str(e))
            return 1

    # async def get_amount(self, user_id: int, order_id: int, date: datetime.date) -> int:
    #     """Общая сумма заказов за дату (с учетом скидок)."""
    #     try:
    #         stmt = select(
    #             func.sum(
    #                 OrdersWB.total_price *
    #                     (1 - OrdersWB.discount_percent / 100)
    #             ).label("total_amount")
    #         ).where(
    #             OrdersWB.user_id == user_id,
    #             cast(OrdersWB.date, Date) == date,
    #             OrdersWB.id < order_id,
    #             OrdersWB.is_cancel.is_(False),
    #         )
    #         result = await self.session.execute(stmt)
    #         total_amount = result.scalar()
    #         return round(total_amount) if total_amount else 0
    #     except SQLAlchemyError as e:
    #         db_logger.error("Error in get_amount", error=str(e))
    #         return 0

    # async def get_total_today(
    #     self,
    #     user_id: int,
    #     order_id: int,
    #     nm_id: int,
    #     date: datetime,  # теперь точное время
    #     total_price: float = 0,
    # ) -> str | int:
    #     try:
    #         if total_price == 0:
    #             raise ValueError("Ответ от сервера отдал 0")

    #         # Начало дня (2025-05-18 00:00:00)
    #         start_of_day = datetime.combine(date.date(), datetime.min.time())

    #         # Запрос по диапазону времени в течение дня
    #         stmt = (
    #             select(
    #                 func.count().label("order_count"),
    #                 func.sum(OrdersWB.total_price * (1 -
    #                          OrdersWB.discount_percent / 100)).label("total_price")
    #             )
    #             .where(
    #                 OrdersWB.user_id == user_id,
    #                 OrdersWB.nm_id == nm_id,
    #                 OrdersWB.is_cancel == False,
    #                 OrdersWB.date >= start_of_day,
    #                 OrdersWB.date <= date
    #             )
    #         )

    #         result = await self.session.execute(stmt)
    #         order_count, db_total_price = result.fetchone()

    #         # Если заказов нет, используем переданный total_price
    #         if order_count == 1 or db_total_price is None:
    #             final_total = total_price
    #         else:
    #             final_total = db_total_price + total_price

    #         return f"{order_count} на {round(final_total)}"

    #     except ValueError as ve:
    #         db_logger.warning(f"Warning: {ve}")
    #         return 0
    #     except (SQLAlchemyError, Exception) as e:
    #         db_logger.error(f"Error in get_total_today: {e}")
    #         return 0

    # async def get_total_yesterday(
    #         self, order_id: int, user_id: int, nm_id: int, date: str) -> str:
    #     """
    #     Получает количество заказов и общую сумму за указанный nmId и дату.
    #     """
    #     try:

    #         # Запрос для фильтрации по nmId и дате, подсчёта заказов и суммы
    #         stmt = (
    #             select(
    #                 func.count().label("order_count"),
    #                 func.sum(
    #                     cast(
    #                         OrdersWB.total_price, Numeric
    #                     ) * (1 - cast(OrdersWB.discount_percent, Numeric) / 100)).label("total_price")
    #             )
    #             .where(
    #                 OrdersWB.user_id == user_id,
    #                 OrdersWB.nm_id == nm_id,
    #                 OrdersWB.is_cancel == False,
    #                 OrdersWB.id < order_id,
    #                 cast(OrdersWB.date, Date) == date
    #             )
    #         )

    #         # Выполнение запроса
    #         result = await self.session.execute(stmt)
    #         order_count, total_price = result.fetchone()
    #         if total_price is None:
    #             total_price = 0

    #         # Формирование результата
    #         return f"{order_count} на {round(total_price)}"

    #     except (SQLAlchemyError, Exception) as e:
    #         db_logger.error(f"Error in get_totals_yesterday: {e}")
    #         return 0

    async def get_totals_combined(
        self,
        user_id: int,
        order_id: int,
        nm_id: int,
        date: datetime,  # точное время
        total_price_today: float = 0,
    ) -> dict:
        try:
            if total_price_today == 0:
                raise ValueError("Ответ от сервера отдал 0")

            start_of_day = datetime.combine(date.date(), datetime.min.time())
            yesterday = date.date() - timedelta(days=1)

            # Подзапрос для сегодняшних данных
            today_subquery = (
                select(
                    func.count().label("order_count"),
                    func.sum(OrdersWB.total_price * (1 -
                                                     OrdersWB.discount_percent / 100)).label("total_price")
                )
                .where(
                    OrdersWB.user_id == user_id,
                    OrdersWB.nm_id == nm_id,
                    OrdersWB.is_cancel == False,
                    OrdersWB.date >= start_of_day,
                    OrdersWB.date <= date
                )
                .subquery()
            )

            # Подзапрос для вчерашних данных
            yesterday_subquery = (
                select(
                    func.count().label("order_count"),
                    func.sum(cast(OrdersWB.total_price, Numeric) * (1 -
                                                                    cast(OrdersWB.discount_percent, Numeric) / 100)).label("total_price")
                )
                .where(
                    OrdersWB.user_id == user_id,
                    OrdersWB.nm_id == nm_id,
                    OrdersWB.is_cancel == False,
                    OrdersWB.id < order_id,
                    cast(OrdersWB.date, Date) == yesterday
                )
                .subquery()
            )

            # Основной запрос через скалярные подзапросы
            stmt = select(
                select(today_subquery.c.order_count).scalar_subquery().label(
                    "today_order_count"),
                select(today_subquery.c.total_price).scalar_subquery().label(
                    "today_total_price"),
                select(yesterday_subquery.c.order_count).scalar_subquery().label(
                    "yesterday_order_count"),
                select(yesterday_subquery.c.total_price).scalar_subquery().label(
                    "yesterday_total_price"),
            )

            result = await self.session.execute(stmt)
            row = result.fetchone()

            # Обработка результатов
            today_orders = row.today_order_count or 0
            today_total = row.today_total_price or 0.0

            if today_orders == 0 or today_total == 0:
                final_today_total = total_price_today
            else:
                final_today_total = today_total + total_price_today

            return f"{today_orders} на {round(final_today_total)}", f"{row.yesterday_order_count or 0} на {round(row.yesterday_total_price or 0)}"

        except ValueError as ve:
            db_logger.warning(f"Warning: {ve}")
            return 0, 0
        except (SQLAlchemyError, Exception) as e:
            db_logger.error(f"Error in get_totals_combined: {e}")
            return 0, 0

    async def stock_stats(self, user_id: int, nm_id: str) -> Optional[str]:
        """
        Получает количество единиц товара на каждом складе по артикулу товара (nmId) 
        с группировкой по складу и дате изменения.
        """
        try:
            # Используем запрос с группировкой по складу и дате, как в SQL
            stmt = (
                select(
                    StocksWB.warehouse_name,
                    func.sum(StocksWB.quantity).label("total_quantity"),
                    StocksWB.last_change_date
                )
                .where(
                    StocksWB.user_id == user_id,
                    StocksWB.nm_id == nm_id,
                    StocksWB.quantity.is_not(None)
                )
                .group_by(StocksWB.warehouse_name, StocksWB.last_change_date)
                .having(func.sum(StocksWB.quantity) > 0)
            )
            
            results = await self.session.execute(stmt)
            stock_data = results.fetchall()

            if not stock_data:
                return f"Остаток для {nm_id}: 0"

            # Группируем по складам и находим последнюю дату для каждого склада
            warehouse_data = defaultdict(list)
            for warehouse, quantity, change_date in stock_data:
                warehouse_data[warehouse].append((quantity, change_date))

            # Для каждого склада берем данные с последней датой
            warehouse_totals = {}
            latest_dates = {}
            
            for warehouse, data_list in warehouse_data.items():
                # Находим последнюю дату для этого склада
                latest_entry = max(data_list, key=lambda x: x[1])
                warehouse_totals[warehouse] = latest_entry[0]
                latest_dates[warehouse] = latest_entry[1]

            # Получаем общее количество
            total_quantity = sum(warehouse_totals.values())

            if total_quantity == 0:
                return f"Остаток для {nm_id}: 0"

            # Находим самую позднюю дату среди всех складов
            overall_latest_date = max(latest_dates.values())

            # Формируем текст
            output = f'Дата обновления: {overall_latest_date.strftime("%Y-%m-%d")}\n'
            for warehouse, quantity in warehouse_totals.items():
                output += f"📦 {warehouse} – {quantity} шт.\n"

            output += f'\n📦 Всего: {total_quantity} шт.'
            return output

        except SQLAlchemyError as e:
            await self.session.rollback()
            db_logger.error(f"Error in stock_stats: {e}")
            return None
