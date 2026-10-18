from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from random import SystemRandom

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from keyboards import admin_home, back_admin
from menu_config import DEFAULT_LABELS, get_menu_labels, set_menu_label
from models import (
    AdminLog,
    DailyBonus,
    Giveaway,
    GiveawayParticipant,
    Order,
    Payment,
    Product,
    Referral,
    Setting,
    Task,
    TaskCompletion,
    Transaction,
    User,
    Withdrawal,
    utcnow,
)
from services.store import (
    approve_manual_task,
    draw_giveaway,
    get_setting,
    grant_giveaway_prize,
    is_admin,
    review_withdrawal,
    set_setting,
)
from states import AdminStates


def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def make_admin_router(session_factory, admin_id: int) -> Router:
    router = Router(name="admin")

    @router.message(Command("admin"))
    async def admin_command(message: Message):
        if not message.from_user or not is_admin(message.from_user.id, admin_id):
            await message.answer("Нет доступа.")
            return
        await message.answer("Админ-панель. Все действия записываются в журнал.", reply_markup=admin_home())

    @router.callback_query(F.data.startswith("adm:"))
    async def admin_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
        # Authorization is checked on every admin callback; hiding buttons is not a security boundary.
        if not is_admin(callback.from_user.id if callback.from_user else None, admin_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        if callback.message is None:
            return
        action = callback.data or ""
        parts = action.split(":")
        section = parts[1] if len(parts) > 1 else ""
        async with session_factory() as session:
            if section == "home":
                await callback.message.answer("Админ-панель:", reply_markup=admin_home())
                return
            if section == "stats":
                users = await session.scalar(select(func.count()).select_from(User)) or 0
                today = await session.scalar(
                    select(func.count()).select_from(User).where(User.created_at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
                ) or 0
                orders = await session.scalar(select(func.count()).select_from(Order)) or 0
                paid = await session.scalar(
                    select(func.count()).select_from(Order).where(
                        Order.status.in_(("paid_pending_fulfillment", "completed"))
                    )
                ) or 0
                pending_delivery = await session.scalar(
                    select(func.count()).select_from(Order).where(Order.status == "paid_pending_fulfillment")
                ) or 0
                giveaways = await session.scalar(
                    select(func.count()).select_from(Giveaway).where(Giveaway.status == "active")
                ) or 0
                await callback.message.answer(
                    "Статистика:\n"
                    f"Пользователей: {users}\nНовых сегодня (UTC): {today}\n"
                    f"Заказов: {orders}\nОплачено: {paid}\n"
                    f"Ожидают выдачи: {pending_delivery}\nАктивных розыгрышей: {giveaways}",
                    reply_markup=back_admin(),
                )
                return
            if section == "users":
                await state.set_state(AdminStates.input)
                await state.update_data(flow="user_search", step="id")
                await callback.message.answer("Введите Telegram ID пользователя для поиска.")
                return
            if section == "user" and len(parts) >= 3:
                target_id = int(parts[2])
                user = await session.get(User, target_id)
                if not user:
                    await callback.message.answer("Пользователь не найден.", reply_markup=back_admin())
                    return
                tx_count = await session.scalar(
                    select(func.count()).select_from(Transaction).where(Transaction.user_id == target_id)
                ) or 0
                orders_count = await session.scalar(
                    select(func.count()).select_from(Order).where(Order.user_id == target_id)
                ) or 0
                await callback.message.answer(
                    f"Пользователь: {target_id}\nИмя: {user.first_name}\n"
                    f"Username: @{user.username or 'нет'}\nБаланс: {user.balance}\n"
                    f"Заблокирован: {'да' if user.is_blocked else 'нет'}\n"
                    f"Заказы: {orders_count} · Операции: {tx_count}",
                    reply_markup=_inline(
                        [
                            [("Заблокировать" if not user.is_blocked else "Разблокировать", f"adm:block:{target_id}")],
                            [("Изменить баланс", f"adm:adjust:{target_id}")],
                            [("История пользователя", f"adm:userhistory:{target_id}")],
                            [("← Админ-меню", "adm:home")],
                        ]
                    ),
                )
                return
            if section == "block" and len(parts) == 3:
                target_id = int(parts[2])
                user = await session.get(User, target_id)
                if not user:
                    await callback.message.answer("Пользователь не найден.", reply_markup=back_admin())
                    return
                user.is_blocked = not user.is_blocked
                session.add(
                    AdminLog(
                        admin_id=admin_id,
                        action="user_blocked" if user.is_blocked else "user_unblocked",
                        target_id=target_id,
                        details="Изменён статус доступа",
                    )
                )
                await session.commit()
                await callback.message.answer(
                    f"Пользователь {'заблокирован' if user.is_blocked else 'разблокирован'}.",
                    reply_markup=_inline([[("Назад", f"adm:user:{target_id}")]]),
                )
                return
            if section == "adjust" and len(parts) == 3:
                await state.set_state(AdminStates.input)
                await state.update_data(flow="balance_adjust", step="amount", target_id=int(parts[2]))
                await callback.message.answer("Введите целое число голды: положительное для начисления, отрицательное для списания.")
                return
            if section == "userhistory" and len(parts) == 3:
                target_id = int(parts[2])
                rows = list(
                    await session.scalars(
                        select(Transaction)
                        .where(Transaction.user_id == target_id)
                        .order_by(Transaction.created_at.desc())
                        .limit(20)
                    )
                )
                text = "\n".join(
                    f"{row.created_at:%Y-%m-%d %H:%M} · {row.amount:+} · {row.reason}"
                    for row in rows
                ) or "Операций нет."
                await callback.message.answer(
                    f"История {target_id}:\n{text[:3500]}",
                    reply_markup=_inline([[("К пользователю", f"adm:user:{target_id}")], [("← Админ-меню", "adm:home")]]),
                )
                return

            if section == "products":
                products = list(await session.scalars(select(Product).order_by(Product.id.desc()).limit(30)))
                rows = [[("＋ Добавить товар", "adm:product-add")]]
                rows.extend(
                    [(f"{'●' if p.is_active else '○'} {p.name} · {p.gold_amount} голды", f"adm:product:{p.id}")]
                    for p in products
                )
                rows.append([("← Админ-меню", "adm:home")])
                await callback.message.answer("Управление товарами:", reply_markup=_inline(rows))
                return
            if section == "product-add":
                await state.set_state(AdminStates.input)
                await state.update_data(flow="product_add", step="name", values={})
                await callback.message.answer("Название товара:")
                return
            if section == "product" and len(parts) == 3:
                product_id = int(parts[2])
                product = await session.get(Product, product_id)
                if not product:
                    await callback.message.answer("Товар не найден.", reply_markup=back_admin())
                    return
                await callback.message.answer(
                    f"{product.name}\n{product.gold_amount} голды · {product.price_stars} ⭐\n"
                    f"Статус: {'включён' if product.is_active else 'выключен'}\n{product.description}",
                    reply_markup=_inline(
                        [
                            [("Изменить количество", f"adm:product-edit:{product_id}:gold")],
                            [("Изменить цену Stars", f"adm:product-edit:{product_id}:stars")],
                            [("Изменить название", f"adm:product-edit:{product_id}:name")],
                            [("Изменить описание", f"adm:product-edit:{product_id}:description")],
                            [("Изменить фото", f"adm:product-edit:{product_id}:photo")],
                            [("Включить/выключить", f"adm:product-toggle:{product_id}")],
                            [("← Товары", "adm:products")],
                        ]
                    ),
                )
                return
            if section == "product-edit" and len(parts) == 4:
                await state.set_state(AdminStates.input)
                await state.update_data(
                    flow="product_edit",
                    step=parts[3],
                    product_id=int(parts[2]),
                )
                await callback.message.answer(
                    {
                        "gold": "Новое количество голды:",
                        "stars": "Новая цена в Telegram Stars:",
                        "name": "Новое название:",
                        "description": "Новое описание:",
                        "photo": "Отправьте новое фото или «-», чтобы удалить:",
                    }[parts[3]]
                )
                return
            if section == "product-toggle" and len(parts) == 3:
                product = await session.get(Product, int(parts[2]))
                if product:
                    product.is_active = not product.is_active
                    session.add(
                        AdminLog(admin_id=admin_id, action="product_toggled", details=f"{product.id}: {product.is_active}")
                    )
                    await session.commit()
                    await callback.message.answer(
                        f"Товар {'включён' if product.is_active else 'выключен'}.",
                        reply_markup=_inline([[("К товару", f"adm:product:{product.id}")], [("← Товары", "adm:products")]]),
                    )
                return

            if section == "orders":
                status_filter = parts[2] if len(parts) > 2 else "all"
                order_query = select(Order)
                if status_filter != "all":
                    order_query = order_query.where(Order.status == status_filter)
                orders = list(
                    await session.scalars(order_query.order_by(Order.created_at.desc()).limit(25))
                )
                labels = {
                    "awaiting_payment": "ожидает оплаты",
                    "paid_pending_fulfillment": "ожидает выдачи",
                    "completed": "выполнен",
                    "cancelled": "отменён",
                    "refunded": "возврат",
                }
                rows = [
                    [("Все", "adm:orders:all"), ("Ожидают оплаты", "adm:orders:awaiting_payment")],
                    [("Ожидают выдачи", "adm:orders:paid_pending_fulfillment"), ("Выполнены", "adm:orders:completed")],
                    [("Отменены/возврат", "adm:orders:cancelled")],
                ]
                rows.extend(
                    [(f"#{o.id} · {o.gold_amount} · {labels.get(o.status, o.status)}", f"adm:order:{o.id}")]
                    for o in orders
                )
                rows.append([("← Админ-меню", "adm:home")])
                await callback.message.answer("Последние заказы:", reply_markup=_inline(rows))
                return
            if section == "order" and len(parts) == 3:
                order_id = parts[2]
                order = await session.get(Order, order_id)
                payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
                if not order:
                    await callback.message.answer("Заказ не найден.", reply_markup=back_admin())
                    return
                buttons = []
                if order.status == "paid_pending_fulfillment":
                    buttons.extend(
                        [[("Отметить выполненным", f"adm:order-complete:{order_id}")],
                         [("Вернуть Stars", f"adm:order-refund:{order_id}")]]
                    )
                elif order.status == "awaiting_payment":
                    buttons.append([("Отменить заказ", f"adm:order-cancel:{order_id}")])
                if payment:
                    payment_text = f"Платёж: {payment.amount} {payment.currency}, {payment.status}"
                else:
                    payment_text = "Платёж не найден."
                buttons.extend([[("← Заказы", "adm:orders")], [("← Админ-меню", "adm:home")]])
                await callback.message.answer(
                    f"Заказ #{order.id}\nПользователь: {order.user_id}\n"
                    f"Товар: {order.product_name} · {order.gold_amount} голды\n"
                    f"Цена: {order.price} {order.currency}\nСтатус: {order.status}\n{payment_text}",
                    reply_markup=_inline(buttons),
                )
                return
            if section == "order-complete" and len(parts) == 3:
                order = await session.get(Order, parts[2])
                if not order or order.status != "paid_pending_fulfillment":
                    await callback.message.answer("Этот заказ нельзя отметить выполненным.", reply_markup=back_admin())
                    return
                order.status = "completed"
                order.updated_at = utcnow()
                session.add(
                    AdminLog(admin_id=admin_id, action="order_completed", target_id=order.user_id, details=order.id)
                )
                await session.commit()
                await bot.send_message(
                    order.user_id,
                    f"Заказ #{order.id} отмечен выполненным администратором. Если возникла проблема, обратитесь в поддержку.",
                )
                await callback.message.answer("Заказ отмечен выполненным. Убедитесь, что голда действительно передана через рынок.")
                return
            if section == "order-cancel" and len(parts) == 3:
                order = await session.get(Order, parts[2])
                if not order or order.status != "awaiting_payment":
                    await callback.message.answer("Можно отменить только неоплаченный заказ.")
                    return
                order.status = "cancelled"
                order.updated_at = utcnow()
                session.add(
                    AdminLog(admin_id=admin_id, action="order_cancelled", target_id=order.user_id, details=order.id)
                )
                await session.commit()
                await bot.send_message(order.user_id, f"Неоплаченный заказ #{order.id} отменён.")
                await callback.message.answer("Заказ отменён.", reply_markup=back_admin())
                return
            if section == "order-refund" and len(parts) == 3:
                order = await session.get(Order, parts[2])
                payment = await session.scalar(select(Payment).where(Payment.order_id == parts[2]))
                if not order or not payment or order.status != "paid_pending_fulfillment" or payment.status != "succeeded":
                    await callback.message.answer("Возврат возможен только для оплаченного заказа, который ещё не выдан.")
                    return
                try:
                    await bot.refund_star_payment(
                        user_id=order.user_id,
                        telegram_payment_charge_id=payment.charge_id,
                    )
                except TelegramAPIError as error:
                    await callback.message.answer(f"Telegram не подтвердил возврат: {str(error)[:500]}")
                    return
                order.status = "refunded"
                order.updated_at = utcnow()
                payment.status = "refunded"
                session.add(
                    AdminLog(admin_id=admin_id, action="star_refund", target_id=order.user_id, details=order.id)
                )
                await session.commit()
                await bot.send_message(order.user_id, f"По заказу #{order.id} выполнен возврат Telegram Stars.")
                await callback.message.answer("Возврат Stars подтверждён Telegram.", reply_markup=back_admin())
                return

            if section == "payments":
                payments = list(
                    await session.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(25))
                )
                text = "\n".join(
                    f"#{p.id} · заказ {p.order_id} · {p.amount} {p.currency} · {p.status} · user {p.user_id}"
                    for p in payments
                ) or "Платежей пока нет."
                await callback.message.answer(text[:3800], reply_markup=back_admin())
                return

            if section == "bonuses":
                enabled = await get_setting(session, "daily_enabled", "true")
                minimum = await get_setting(session, "daily_min", "5")
                maximum = await get_setting(session, "daily_max", "50")
                period = await get_setting(session, "daily_period_hours", "24")
                await callback.message.answer(
                    f"Ежедневный бонус: {'включён' if enabled == 'true' else 'выключен'}\n"
                    f"Награда: {minimum}–{maximum} голды\nПериод: {period} ч.",
                    reply_markup=_inline(
                        [
                            [("Включить/выключить", "adm:bonus-toggle")],
                            [("Изменить минимум", "adm:bonus-min"), ("Изменить максимум", "adm:bonus-max")],
                            [("Изменить период", "adm:bonus-period")],
                            [("← Админ-меню", "adm:home")],
                        ]
                    ),
                )
                return
            if section == "bonus-toggle":
                enabled = await get_setting(session, "daily_enabled", "true")
                await set_setting(session, "daily_enabled", "false" if enabled == "true" else "true")
                session.add(AdminLog(admin_id=admin_id, action="daily_bonus_toggle", details=f"{enabled} -> {not enabled == 'true'}"))
                await session.commit()
                await callback.message.answer("Настройка бонуса обновлена.", reply_markup=_inline([[("К бонусам", "adm:bonuses")]]))
                return
            if section in {"bonus-min", "bonus-max", "bonus-period"}:
                flow = {"bonus-min": "bonus_min", "bonus-max": "bonus_max", "bonus-period": "bonus_period"}[section]
                await state.set_state(AdminStates.input)
                await state.update_data(flow=flow, step="value")
                await callback.message.answer("Введите целое значение:")
                return

            if section == "giveaways":
                giveaways = list(await session.scalars(select(Giveaway).order_by(Giveaway.id.desc()).limit(20)))
                rows = [[("＋ Создать розыгрыш", "adm:giveaway-add")]]
                for giveaway in giveaways:
                    rows.append(
                        [(f"{giveaway.title} · {giveaway.status}", f"adm:giveaway:{giveaway.id}")]
                    )
                rows.append([("← Админ-меню", "adm:home")])
                await callback.message.answer("Розыгрыши:", reply_markup=_inline(rows))
                return
            if section == "giveaway-add":
                await state.set_state(AdminStates.input)
                await state.update_data(flow="giveaway_add", step="title", values={})
                await callback.message.answer("Название розыгрыша:")
                return
            if section == "giveaway" and len(parts) == 3:
                giveaway_id = int(parts[2])
                giveaway = await session.get(Giveaway, giveaway_id)
                if not giveaway:
                    await callback.message.answer("Розыгрыш не найден.", reply_markup=back_admin())
                    return
                participants = await session.scalar(
                    select(func.count()).select_from(GiveawayParticipant).where(
                        GiveawayParticipant.giveaway_id == giveaway_id
                    )
                ) or 0
                winners = list(
                    await session.scalars(
                        select(GiveawayParticipant).where(
                            GiveawayParticipant.giveaway_id == giveaway_id,
                            GiveawayParticipant.is_winner.is_(True),
                        )
                    )
                )
                rows = []
                if giveaway.status == "active":
                    rows.extend(
                        [
                            [("Изменить название", f"adm:giveaway-edit:{giveaway_id}:title")],
                            [("Изменить описание", f"adm:giveaway-edit:{giveaway_id}:description")],
                            [("Изменить приз", f"adm:giveaway-edit:{giveaway_id}:prize")],
                            [("Изменить число победителей", f"adm:giveaway-edit:{giveaway_id}:winners")],
                            [("Изменить дату завершения", f"adm:giveaway-edit:{giveaway_id}:ends_at")],
                            [("Список участников", f"adm:giveaway-participants:{giveaway_id}")],
                            [("Завершить и выбрать победителей", f"adm:giveaway-draw:{giveaway_id}")],
                        ]
                    )
                for winner in winners:
                    if not winner.prize_delivered:
                        rows.append(
                            [(f"Выдать приз user {winner.user_id}", f"adm:giveaway-grant:{giveaway_id}:{winner.user_id}")]
                        )
                rows.extend([[("← Розыгрыши", "adm:giveaways")], [("← Админ-меню", "adm:home")]])
                await callback.message.answer(
                    f"{giveaway.title}\n{giveaway.description}\nПриз: {giveaway.prize} голды · "
                    f"Победителей: {giveaway.winners_count}\nУчастников: {participants}\n"
                    f"Окончание UTC: {giveaway.ends_at:%Y-%m-%d %H:%M}\n"
                    f"Победители: {', '.join(str(x.user_id) for x in winners) or 'ещё не выбраны'}",
                    reply_markup=_inline(rows),
                )
                return
            if section == "giveaway-edit" and len(parts) == 4:
                giveaway_id, field = int(parts[2]), parts[3]
                prompts = {
                    "title": "Новое название розыгрыша:",
                    "description": "Новое описание:",
                    "prize": "Новый приз в голде (целое число больше нуля):",
                    "winners": "Новое количество победителей:",
                    "ends_at": "Новая дата завершения в UTC: ГГГГ-ММ-ДД ЧЧ:ММ",
                }
                if field not in prompts:
                    await callback.message.answer("Неизвестное поле.", reply_markup=back_admin())
                    return
                giveaway = await session.get(Giveaway, giveaway_id)
                if not giveaway or giveaway.status != "active":
                    await callback.message.answer(
                        "Изменять можно только активный розыгрыш.", reply_markup=back_admin()
                    )
                    return
                await state.set_state(AdminStates.input)
                await state.update_data(flow="giveaway_edit", step=field, giveaway_id=giveaway_id)
                await callback.message.answer(prompts[field])
                return
            if section == "giveaway-participants" and len(parts) == 3:
                giveaway_id = int(parts[2])
                participants = list(
                    await session.scalars(
                        select(GiveawayParticipant)
                        .where(GiveawayParticipant.giveaway_id == giveaway_id)
                        .order_by(GiveawayParticipant.joined_at)
                        .limit(100)
                    )
                )
                text = "\n".join(
                    f"{p.user_id}{' — победитель' if p.is_winner else ''}" for p in participants
                ) or "Участников нет."
                await callback.message.answer(
                    f"Участники (первые 100):\n{text[:3500]}",
                    reply_markup=_inline([[("← Розыгрыш", f"adm:giveaway:{giveaway_id}")]]),
                )
                return
            if section == "giveaway-draw" and len(parts) == 3:
                giveaway_id = int(parts[2])
                created, winners, error = await draw_giveaway(session_factory, giveaway_id)
                if error:
                    await callback.message.answer(error, reply_markup=back_admin())
                    return
                async with session_factory() as check_session:
                    giveaway = await check_session.get(Giveaway, giveaway_id)
                result_text = (
                    f"Розыгрыш «{giveaway.title}» завершён. "
                    + (f"Победители: {', '.join(map(str, winners))}." if winners else "Участников не было.")
                )
                async with session_factory() as check_session:
                    participant_ids = list(
                        await check_session.scalars(
                            select(GiveawayParticipant.user_id).where(
                                GiveawayParticipant.giveaway_id == giveaway_id
                            )
                        )
                    )
                for target in participant_ids:
                    try:
                        await bot.send_message(target, result_text)
                        await asyncio.sleep(0.05)
                    except TelegramAPIError:
                        continue
                async with session_factory() as log_session, log_session.begin():
                    log_session.add(
                        AdminLog(admin_id=admin_id, action="giveaway_draw", details=f"{giveaway_id}: {winners}")
                    )
                await callback.message.answer(result_text, reply_markup=_inline([[("Открыть розыгрыш", f"adm:giveaway:{giveaway_id}")]]))
                return
            if section == "giveaway-grant" and len(parts) == 4:
                giveaway_id, target_id = int(parts[2]), int(parts[3])
                ok, balance, error = await grant_giveaway_prize(session_factory, giveaway_id, target_id)
                if ok:
                    async with session_factory() as log_session, log_session.begin():
                        log_session.add(
                            AdminLog(admin_id=admin_id, action="giveaway_prize_granted", target_id=target_id, details=f"Giveaway {giveaway_id}")
                        )
                    await bot.send_message(target_id, f"Администратор выдал приз розыгрыша. Баланс: {balance} голды.")
                    await callback.message.answer("Приз начислен на внутренний баланс.", reply_markup=back_admin())
                else:
                    await callback.message.answer(error, reply_markup=back_admin())
                return

            if section == "withdrawals":
                requests = list(
                    await session.scalars(select(Withdrawal).order_by(Withdrawal.created_at.desc()).limit(25))
                )
                rows = [
                    [(f"#{r.id} · {r.amount} · {r.status} · user {r.user_id}", f"adm:withdrawal:{r.id}")]
                    for r in requests
                ]
                rows.append([("← Админ-меню", "adm:home")])
                await callback.message.answer("Заявки на выдачу:", reply_markup=_inline(rows))
                return
            if section == "withdrawal" and len(parts) == 3:
                request = await session.get(Withdrawal, int(parts[2]))
                if not request:
                    await callback.message.answer("Заявка не найдена.", reply_markup=back_admin())
                    return
                rows = []
                if request.status in {"new", "reviewing"}:
                    rows.extend(
                        [[("Взять в работу", f"adm:withdrawal-review:{request.id}")],
                         [("Одобрить", f"adm:withdrawal-approve:{request.id}")],
                         [("Отклонить", f"adm:withdrawal-reject:{request.id}")]]
                    )
                elif request.status == "approved":
                    rows.extend(
                        [[("Отметить выданной", f"adm:withdrawal-complete:{request.id}")],
                         [("Отклонить и вернуть баланс", f"adm:withdrawal-reject:{request.id}")]]
                    )
                rows.extend([[("← Заявки", "adm:withdrawals")], [("← Админ-меню", "adm:home")]])
                await callback.message.answer(
                    f"Заявка #{request.id}\nПользователь: {request.user_id}\n"
                    f"Количество: {request.amount} голды\nСтатус: {request.status}\n"
                    f"Лот: {request.lot_details}",
                    reply_markup=_inline(rows),
                )
                return
            if section == "withdrawal-review" and len(parts) == 3:
                await _withdrawal_action(callback, bot, session_factory, admin_id, int(parts[2]), "review")
                return
            if section == "withdrawal-approve" and len(parts) == 3:
                await _withdrawal_action(callback, bot, session_factory, admin_id, int(parts[2]), "approve")
                return
            if section == "withdrawal-reject" and len(parts) == 3:
                await _withdrawal_action(callback, bot, session_factory, admin_id, int(parts[2]), "reject")
                return
            if section == "withdrawal-complete" and len(parts) == 3:
                await _withdrawal_action(callback, bot, session_factory, admin_id, int(parts[2]), "complete")
                return

            if section == "tasks":
                tasks = list(await session.scalars(select(Task).order_by(Task.id.desc()).limit(30)))
                pending = list(
                    await session.scalars(
                        select(TaskCompletion).where(TaskCompletion.status == "pending").limit(15)
                    )
                )
                rows = [[("＋ Добавить задание", "adm:task-add")]]
                rows.extend(
                    [(f"{'●' if t.is_active else '○'} {t.title} · {t.reward}", f"adm:task:{t.id}")]
                    for t in tasks
                )
                rows.extend(
                    [(f"Проверить #{p.id} · task {p.task_id} · user {p.user_id}", f"adm:task-review:{p.task_id}:{p.user_id}")]
                    for p in pending
                )
                rows.append([("← Админ-меню", "adm:home")])
                await callback.message.answer("Задания и ручные проверки:", reply_markup=_inline(rows))
                return
            if section == "task-add":
                await callback.message.answer(
                    "Выберите тип задания:",
                    reply_markup=_inline(
                        [[("Подписка на канал", "adm:task-type:channel"), ("Ручная проверка", "adm:task-type:manual")],
                         [("← Задания", "adm:tasks")]]
                    ),
                )
                return
            if section == "task-type" and len(parts) == 3 and parts[2] in {"channel", "manual"}:
                await state.set_state(AdminStates.input)
                await state.update_data(flow="task_add", step="title", values={"task_type": parts[2]})
                await callback.message.answer("Название задания:")
                return
            if section == "task" and len(parts) == 3:
                task = await session.get(Task, int(parts[2]))
                if not task:
                    await callback.message.answer("Задание не найдено.", reply_markup=back_admin())
                    return
                rows = [
                    [("Изменить заголовок", f"adm:task-edit:{task.id}:title")],
                    [("Изменить текст задания", f"adm:task-edit:{task.id}:description")],
                    [("Изменить текст кнопки", f"adm:task-edit:{task.id}:button_text")],
                    [("Изменить награду", f"adm:task-edit:{task.id}:reward")],
                ]
                if task.task_type == "channel":
                    rows.append([("Изменить канал", f"adm:task-edit:{task.id}:channel")])
                rows.extend(
                    [
                        [("Включить/выключить", f"adm:task-toggle:{task.id}")],
                        [("← Задания", "adm:tasks")],
                    ]
                )
                await callback.message.answer(
                    f"{task.title}\nТип: {task.task_type}\nКанал: {task.channel or 'нет'}\n"
                    f"Награда: {task.reward}\nСтатус: {'включено' if task.is_active else 'выключено'}",
                    reply_markup=_inline(rows),
                )
                return
            if section == "task-edit" and len(parts) == 4:
                task_id, field = int(parts[2]), parts[3]
                prompts = {
                    "title": "Новый заголовок задания:",
                    "description": "Введите текст под заголовком. Можно использовать {channel} и {reward}. Пустой текст отключить нельзя — отправьте дефис -, чтобы вернуть стандартный текст:",
                    "button_text": "Текст кнопки (до 64 символов). Дефис - вернёт стандартный текст:",
                    "reward": "Новая награда (целое число больше нуля):",
                    "channel": "Новый @username или chat ID канала:",
                }
                if field not in prompts:
                    await callback.message.answer("Неизвестное поле.", reply_markup=back_admin())
                    return
                task = await session.get(Task, task_id)
                if not task or not task.is_active:
                    await callback.message.answer(
                        "Редактировать можно только включённое задание.", reply_markup=back_admin()
                    )
                    return
                await state.set_state(AdminStates.input)
                await state.update_data(flow="task_edit", step=field, task_id=task_id)
                await callback.message.answer(prompts[field])
                return
            if section == "task-toggle" and len(parts) == 3:
                task = await session.get(Task, int(parts[2]))
                if task:
                    task.is_active = not task.is_active
                    session.add(AdminLog(admin_id=admin_id, action="task_toggled", details=f"{task.id}: {task.is_active}"))
                    await session.commit()
                    await callback.message.answer("Статус задания изменён.", reply_markup=back_admin())
                return
            if section == "task-review" and len(parts) == 4:
                task_id, target_id = int(parts[2]), int(parts[3])
                completion = await session.scalar(
                    select(TaskCompletion).where(
                        TaskCompletion.task_id == task_id,
                        TaskCompletion.user_id == target_id,
                    )
                )
                task = await session.get(Task, task_id)
                if not completion or not task:
                    await callback.message.answer("Заявка не найдена.", reply_markup=back_admin())
                    return
                await callback.message.answer(
                    f"Задание: {task.title}\nПользователь: {target_id}\n"
                    f"Статус: {completion.status}\nПодтверждение: {completion.proof or 'см. сообщение выше'}",
                    reply_markup=_inline(
                        [[("Подтвердить и начислить", f"adm:task-approve:{task_id}:{target_id}")],
                         [("Отклонить", f"adm:task-reject:{task_id}:{target_id}")],
                         [("← Задания", "adm:tasks")]]
                    ),
                )
                return
            if section in {"task-approve", "task-reject"} and len(parts) == 4:
                task_id, target_id = int(parts[2]), int(parts[3])
                ok, error = await approve_manual_task(
                    session_factory,
                    task_id,
                    target_id,
                    admin_id,
                    approved=section == "task-approve",
                )
                if ok:
                    await bot.send_message(
                        target_id,
                        "Задание подтверждено, награда начислена."
                        if section == "task-approve"
                        else "Заявка на задание отклонена.",
                    )
                    await callback.message.answer("Результат проверки сохранён.", reply_markup=back_admin())
                else:
                    await callback.message.answer(error, reply_markup=back_admin())
                return

            if section == "referrals":
                reward = await get_setting(session, "referral_reward", "10")
                referrals = await session.scalar(select(func.count()).select_from(Referral)) or 0
                await callback.message.answer(
                    f"Всего приглашений: {referrals}\nНаграда за приглашение: {reward} голды.\n"
                    "Начисление происходит только при первом запуске новым пользователем.",
                    reply_markup=_inline(
                        [[("Изменить награду", "adm:referral-reward")], [("← Админ-меню", "adm:home")]]
                    ),
                )
                return
            if section == "referral-reward":
                await state.set_state(AdminStates.input)
                await state.update_data(flow="referral_reward", step="value")
                await callback.message.answer("Введите целую неотрицательную награду:")
                return

            if section == "broadcast":
                await state.set_state(AdminStates.input)
                await state.update_data(flow="broadcast", step="message")
                await callback.message.answer("Отправьте текст рассылки. Сначала вы увидите предпросмотр.")
                return
            if section == "broadcast-cancel":
                await state.clear()
                await callback.message.answer("Рассылка отменена.", reply_markup=back_admin())
                return
            if section == "broadcast-send":
                data = await state.get_data()
                if data.get("flow") != "broadcast" or not data.get("text"):
                    await state.clear()
                    await callback.message.answer("Черновик рассылки не найден.", reply_markup=back_admin())
                    return
                recipients = []
                async with session_factory() as users_session:
                    recipients = list(
                        await users_session.scalars(select(User.telegram_id).where(User.is_blocked.is_(False)))
                    )
                sent = failed = 0
                for target_id in recipients:
                    try:
                        await bot.send_message(target_id, data["text"])
                        sent += 1
                    except TelegramAPIError:
                        failed += 1
                    await asyncio.sleep(0.05)
                async with session_factory() as log_session, log_session.begin():
                    log_session.add(
                        AdminLog(
                            admin_id=admin_id,
                            action="broadcast_sent",
                            details=f"sent={sent}, failed={failed}",
                        )
                    )
                await state.clear()
                await callback.message.answer(
                    f"Рассылка завершена. Доставлено: {sent}, ошибок: {failed}.",
                    reply_markup=back_admin(),
                )
                return

            if section == "menu-labels":
                labels = get_menu_labels()
                keys = [("daily", "Ежедневный бонус"), ("open", "Открыть бонус"), ("giveaways", "Розыгрыши"), ("store", "Купить голду"), ("balance", "Мой баланс"), ("withdraw", "Вывод голды"), ("orders", "Мои заказы"), ("tasks", "Задания"), ("referral", "Пригласить друга"), ("history", "История операций"), ("support", "Поддержка")]
                rows = [[(f"{name}: {labels[key]}", f"adm:menu-edit:{key}")] for key, name in keys]
                rows.append([("← Админ-меню", "adm:home")])
                await callback.message.answer("Выбери кнопку. Можно изменить текст и добавить любой смайлик в начале:", reply_markup=_inline(rows))
                return
            if section == "menu-edit" and len(parts) == 3 and parts[2] in DEFAULT_LABELS:
                key = parts[2]
                await state.set_state(AdminStates.input)
                await state.update_data(flow="menu_label_edit", menu_key=key)
                await callback.message.answer(f"Отправь новый текст кнопки для «{key}». Можно добавить emoji, например: 📤 Вывод голды. До 64 символов. Чтобы вернуть стандартный текст, отправь дефис -.")
                return
            if section == "settings":
                await callback.message.answer(
                    "Настройки:\n"
                    f"ADMIN_ID: {admin_id}\n"
                    "CryptoBot: отключён. Telegram требует оплачивать цифровые товары ботов только Stars (XTR).\n"
                    "Для добавления канала в задание укажите @username или доступный боту chat ID.",
                    reply_markup=back_admin(),
                )
                return
            if section == "logs":
                rows = list(await session.scalars(select(AdminLog).order_by(AdminLog.created_at.desc()).limit(25)))
                text = "\n".join(
                    f"{row.created_at:%Y-%m-%d %H:%M} · {row.admin_id} · {row.action} · "
                    f"{row.target_id or ''} · {row.details[:120]}"
                    for row in rows
                ) or "Записей пока нет."
                await callback.message.answer(text[:3800], reply_markup=back_admin())
                return
            if section == "supportreply" and len(parts) == 3:
                await state.set_state(AdminStates.input)
                await state.update_data(flow="support_reply", target_id=int(parts[2]))
                await callback.message.answer(f"Введите ответ пользователю {parts[2]}:")
                return

    @router.message(AdminStates.input)
    async def admin_input(message: Message, state: FSMContext, bot: Bot):
        if not message.from_user or not is_admin(message.from_user.id, admin_id):
            await state.clear()
            await message.answer("Нет доступа.")
            return
        data = await state.get_data()
        flow, step = data.get("flow"), data.get("step")
        if not flow:
            await state.clear()
            return
        text = (message.text or "").strip()

        if flow == "menu_label_edit":
            key = data.get("menu_key")
            if key not in DEFAULT_LABELS:
                await state.clear()
                await message.answer("Неизвестная кнопка.", reply_markup=admin_home())
                return
            defaults = get_menu_labels()
            value = DEFAULT_LABELS[key] if text == "-" else text
            if not value or len(value) > 64:
                await message.answer("Текст кнопки должен содержать от 1 до 64 символов.")
                return
            try:
                set_menu_label(key, value)
            except OSError:
                await message.answer("Не удалось сохранить настройки. Проверь права на запись файлов.", reply_markup=admin_home())
                await state.clear()
                return
            await state.clear()
            await message.answer(f"Кнопка обновлена: {value}\nОткрой главное меню пользователя заново, чтобы увидеть изменения.", reply_markup=admin_home())
            return

        if flow == "product_add":
            values = dict(data.get("values", {}))
            if step == "name":
                if not text or len(text) > 120:
                    await message.answer("Название должно содержать от 1 до 120 символов.")
                    return
                values["name"] = text
                next_step, prompt = "gold", "Количество голды (целое число больше нуля):"
            elif step == "gold":
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                values["gold_amount"] = int(text)
                next_step, prompt = "stars", "Цена в Telegram Stars (целое число больше нуля):"
            elif step == "stars":
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                values["price_stars"] = int(text)
                next_step, prompt = "description", "Описание товара:"
            elif step == "description":
                values["description"] = text[:1000]
                next_step, prompt = "photo", "Отправьте фото товара или отправьте «-», чтобы пропустить:"
            elif step == "photo":
                if message.photo:
                    values["photo_file_id"] = message.photo[-1].file_id
                elif text == "-":
                    values["photo_file_id"] = None
                else:
                    await message.answer("Отправьте фотографию или символ «-».")
                    return
                async with session_factory() as session, session.begin():
                    product = Product(
                        name=values["name"],
                        gold_amount=values["gold_amount"],
                        price_stars=values["price_stars"],
                        description=values.get("description", ""),
                        photo_file_id=values.get("photo_file_id"),
                        price_crypto=0,
                        is_active=True,
                    )
                    session.add(product)
                    await session.flush()
                    session.add(
                        AdminLog(
                            admin_id=admin_id,
                            action="product_created",
                            details=f"{product.id}: {product.name}",
                        )
                    )
                await state.clear()
                await message.answer("Товар добавлен. Способ оплаты: только Telegram Stars.", reply_markup=admin_home())
                return
            await state.update_data(values=values, step=next_step)
            await message.answer(prompt)
            return

        if flow == "product_edit":
            product_id = int(data["product_id"])
            async with session_factory() as session, session.begin():
                product = await session.get(Product, product_id)
                if not product:
                    await state.clear()
                    await message.answer("Товар не найден.", reply_markup=admin_home())
                    return
                if step in {"gold", "stars"}:
                    if not text.isdigit() or int(text) <= 0:
                        await message.answer("Введите положительное целое число.")
                        return
                    setattr(product, "gold_amount" if step == "gold" else "price_stars", int(text))
                elif step == "description":
                    product.description = text[:1000]
                elif step == "name":
                    if not text or len(text) > 120:
                        await message.answer("Название должно содержать от 1 до 120 символов.")
                        return
                    product.name = text
                elif step == "photo":
                    if message.photo:
                        product.photo_file_id = message.photo[-1].file_id
                    elif text == "-":
                        product.photo_file_id = None
                    else:
                        await message.answer("Отправьте фотографию или символ «-».")
                        return
                session.add(
                    AdminLog(admin_id=admin_id, action="product_updated", details=f"{product_id}: {step}")
                )
            await state.clear()
            await message.answer("Товар обновлён.", reply_markup=admin_home())
            return

        if flow == "user_search":
            if not text.isdigit():
                await message.answer("Введите числовой Telegram ID.")
                return
            target_id = int(text)
            await state.clear()
            async with session_factory() as session:
                user = await session.get(User, target_id)
                tx_count = await session.scalar(
                    select(func.count()).select_from(Transaction).where(Transaction.user_id == target_id)
                ) or 0
                orders_count = await session.scalar(
                    select(func.count()).select_from(Order).where(Order.user_id == target_id)
                ) or 0
            if not user:
                await message.answer("Пользователь не найден.", reply_markup=admin_home())
                return
            await message.answer(
                f"Пользователь: {target_id}\nИмя: {user.first_name}\n"
                f"Username: @{user.username or 'нет'}\nБаланс: {user.balance}\n"
                f"Заблокирован: {'да' if user.is_blocked else 'нет'}\n"
                f"Заказы: {orders_count} · Операции: {tx_count}",
                reply_markup=_inline(
                    [[("Изменить баланс", f"adm:adjust:{target_id}")],
                     [("Заблокировать" if not user.is_blocked else "Разблокировать", f"adm:block:{target_id}")],
                     [("История", f"adm:userhistory:{target_id}")],
                     [("← Админ-меню", "adm:home")]]
                ),
            )
            return

        if flow == "balance_adjust":
            if step == "amount":
                try:
                    amount = int(text)
                except ValueError:
                    await message.answer("Введите целое число, например 25 или -10.")
                    return
                await state.update_data(amount=amount, step="reason")
                await message.answer("Укажите обязательную причину изменения баланса:")
                return
            if step == "reason":
                if not text or len(text) > 250:
                    await message.answer("Причина обязательна и должна быть не длиннее 250 символов.")
                    return
                target_id, amount = int(data["target_id"]), int(data["amount"])
                if amount == 0:
                    await message.answer("Нулевое изменение не требуется. Начните заново.")
                    await state.clear()
                    return
                async with session_factory() as session:
                    user = await session.get(User, target_id)
                if not user:
                    await state.clear()
                    await message.answer("Пользователь не найден.", reply_markup=admin_home())
                    return
                async with session_factory() as session, session.begin():
                    user = await session.get(User, target_id)
                    if user.balance + amount < 0:
                        await message.answer("На балансе недостаточно средств для списания.")
                        return
                    user.balance += amount
                    session.add(
                        Transaction(
                            user_id=target_id,
                            amount=amount,
                            kind="admin_adjustment",
                            reason=text[:250],
                            reference=f"admin-balance:{target_id}:{utcnow().isoformat()}",
                        )
                    )
                    session.add(
                        AdminLog(
                            admin_id=admin_id,
                            action="balance_adjustment",
                            target_id=target_id,
                            details=f"{amount:+} голды: {text[:200]}",
                        )
                    )
                    new_balance = user.balance
                await state.clear()
                try:
                    await bot.send_message(
                        target_id,
                        f"Администратор изменил баланс на {amount:+} голды. Причина: {text[:250]}. Новый баланс: {new_balance}.",
                    )
                except TelegramAPIError:
                    pass
                await message.answer("Изменение баланса записано.", reply_markup=admin_home())
                return

        if flow == "giveaway_edit":
            field = step
            if field in {"prize", "winners"}:
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                value = int(text)
            elif field == "title":
                if not text or len(text) > 160:
                    await message.answer("Название должно содержать от 1 до 160 символов.")
                    return
                value = text
            elif field == "description":
                value = text[:2000]
            elif field == "ends_at":
                try:
                    value = datetime.strptime(text, "%Y-%m-%d %H:%M")
                except ValueError:
                    await message.answer("Формат: ГГГГ-ММ-ДД ЧЧ:ММ, время указывается в UTC.")
                    return
                if value <= utcnow():
                    await message.answer("Дата завершения должна быть в будущем.")
                    return
            else:
                await state.clear()
                await message.answer("Неизвестное поле.", reply_markup=admin_home())
                return
            async with session_factory() as session, session.begin():
                giveaway = await session.get(Giveaway, int(data["giveaway_id"]))
                if not giveaway or giveaway.status != "active":
                    await state.clear()
                    await message.answer("Розыгрыш больше нельзя редактировать.", reply_markup=admin_home())
                    return
                column = {"winners": "winners_count"}.get(field, field)
                setattr(giveaway, column, value)
                session.add(
                    AdminLog(
                        admin_id=admin_id,
                        action="giveaway_updated",
                        details=f"{giveaway.id}: {field}",
                    )
                )
            await state.clear()
            await message.answer("Розыгрыш обновлён.", reply_markup=admin_home())
            return

        if flow == "giveaway_add":
            values = dict(data.get("values", {}))
            if step == "title":
                if not text or len(text) > 160:
                    await message.answer("Название должно содержать от 1 до 160 символов.")
                    return
                values["title"] = text
                next_step, prompt = "description", "Описание розыгрыша:"
            elif step == "description":
                values["description"] = text[:2000]
                next_step, prompt = "prize", "Размер приза в голде (целое число больше нуля):"
            elif step == "prize":
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                values["prize"] = int(text)
                next_step, prompt = "winners", "Количество победителей (целое число больше нуля):"
            elif step == "winners":
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                values["winners_count"] = int(text)
                next_step, prompt = "ends_at", "Дата завершения в UTC: ГГГГ-ММ-ДД ЧЧ:ММ"
            elif step == "ends_at":
                try:
                    ends_at = datetime.strptime(text, "%Y-%m-%d %H:%M")
                except ValueError:
                    await message.answer("Формат: ГГГГ-ММ-ДД ЧЧ:ММ, время указывается в UTC.")
                    return
                if ends_at <= utcnow():
                    await message.answer("Дата завершения должна быть в будущем.")
                    return
                async with session_factory() as session, session.begin():
                    giveaway = Giveaway(
                        title=values["title"],
                        description=values.get("description", ""),
                        prize=values["prize"],
                        winners_count=values["winners_count"],
                        ends_at=ends_at,
                        status="active",
                    )
                    session.add(giveaway)
                    await session.flush()
                    session.add(
                        AdminLog(admin_id=admin_id, action="giveaway_created", details=f"{giveaway.id}: {giveaway.title}")
                    )
                await state.clear()
                await message.answer("Розыгрыш создан.", reply_markup=admin_home())
                return
            await state.update_data(values=values, step=next_step)
            await message.answer(prompt)
            return

        if flow == "task_edit":
            field = step
            if field == "title":
                if not text or len(text) > 160:
                    await message.answer("Название должно содержать от 1 до 160 символов.")
                    return
                value = text
            elif field == "reward":
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                value = int(text)
            elif field == "description":
                value = "" if text == "-" else text
                if len(value) > 2000:
                    await message.answer("Текст должен быть не длиннее 2000 символов.")
                    return
            elif field == "button_text":
                value = "" if text == "-" else text
                if len(value) > 64:
                    await message.answer("Текст кнопки должен быть не длиннее 64 символов.")
                    return
            elif field == "channel":
                if not text.startswith("@") and not text.lstrip("-").isdigit():
                    await message.answer("Укажите @username или числовой chat ID канала.")
                    return
                value = text
            else:
                await state.clear()
                await message.answer("Неизвестное поле.", reply_markup=admin_home())
                return
            async with session_factory() as session, session.begin():
                task = await session.get(Task, int(data["task_id"]))
                if not task or not task.is_active:
                    await state.clear()
                    await message.answer("Задание больше нельзя редактировать.", reply_markup=admin_home())
                    return
                setattr(task, field, value)
                session.add(
                    AdminLog(admin_id=admin_id, action="task_updated", details=f"{task.id}: {field}")
                )
            await state.clear()
            await message.answer("Задание обновлено.", reply_markup=admin_home())
            return

        if flow == "task_add":
            values = dict(data.get("values", {}))
            task_type = values.get("task_type", "manual")
            if step == "title":
                if not text or len(text) > 160:
                    await message.answer("Название должно содержать от 1 до 160 символов.")
                    return
                values["title"] = text
                if task_type == "channel":
                    next_step, prompt = "channel", "Укажите @username или chat ID канала:"
                else:
                    values["channel"] = None
                    next_step, prompt = "reward", "Награда в голде (целое число больше нуля):"
            elif step == "channel":
                if not text.startswith("@") and not text.lstrip("-").isdigit():
                    await message.answer("Укажите @username или числовой chat ID канала.")
                    return
                values["channel"] = text
                next_step, prompt = "reward", "Награда в голде (целое число больше нуля):"
            elif step == "reward":
                if not text.isdigit() or int(text) <= 0:
                    await message.answer("Введите положительное целое число.")
                    return
                async with session_factory() as session, session.begin():
                    task = Task(
                        title=values["title"],
                        task_type=task_type,
                        channel=values.get("channel"),
                        reward=int(text),
                        is_active=True,
                    )
                    session.add(task)
                    await session.flush()
                    session.add(AdminLog(admin_id=admin_id, action="task_created", details=f"{task.id}: {task.title}"))
                await state.clear()
                await message.answer("Задание создано.", reply_markup=admin_home())
                return
            await state.update_data(values=values, step=next_step)
            await message.answer(prompt)
            return

        if flow in {"bonus_min", "bonus_max", "bonus_period", "referral_reward"}:
            if not text.isdigit():
                await message.answer("Введите неотрицательное целое число.")
                return
            value = int(text)
            key = {
                "bonus_min": "daily_min",
                "bonus_max": "daily_max",
                "bonus_period": "daily_period_hours",
                "referral_reward": "referral_reward",
            }[flow]
            if flow == "bonus_period" and value < 1:
                await message.answer("Период должен быть не меньше 1 часа.")
                return
            async with session_factory() as session, session.begin():
                if flow == "bonus_min":
                    maximum = int(await get_setting(session, "daily_max", "50"))
                    if value > maximum:
                        await message.answer("Минимум не может превышать максимум.")
                        return
                if flow == "bonus_max":
                    minimum = int(await get_setting(session, "daily_min", "5"))
                    if value < minimum:
                        await message.answer("Максимум не может быть меньше минимума.")
                        return
                await set_setting(session, key, str(value))
                session.add(AdminLog(admin_id=admin_id, action=f"setting_{key}", details=str(value)))
            await state.clear()
            await message.answer("Настройка сохранена.", reply_markup=admin_home())
            return

        if flow == "broadcast":
            if not text or len(text) > 4000:
                await message.answer("Введите сообщение длиной от 1 до 4000 символов.")
                return
            await state.update_data(text=text)
            await message.answer(
                f"Предпросмотр рассылки:\n\n{text}\n\nОтправить всем пользователям?",
                reply_markup=_inline([[("Отправить", "adm:broadcast-send"), ("Отмена", "adm:broadcast-cancel")]]),
            )
            return

        if flow == "support_reply":
            target_id = int(data["target_id"])
            if not text:
                await message.answer("Ответ не может быть пустым.")
                return
            try:
                await bot.send_message(target_id, f"Ответ поддержки:\n{text[:3000]}")
            except TelegramAPIError:
                await message.answer("Не удалось доставить ответ пользователю.")
                return
            async with session_factory() as session, session.begin():
                session.add(
                    AdminLog(admin_id=admin_id, action="support_reply", target_id=target_id, details=text[:200])
                )
            await state.clear()
            await message.answer("Ответ отправлен.", reply_markup=admin_home())
            return

        await state.clear()
        await message.answer("Шаг не распознан. Откройте /admin и начните заново.", reply_markup=admin_home())

    return router


async def _withdrawal_action(callback, bot, session_factory, admin_id: int, request_id: int, action: str) -> None:
    ok, request, error = await review_withdrawal(session_factory, request_id, action, admin_id)
    if not ok:
        await callback.message.answer(error, reply_markup=back_admin())
        return
    user_messages = {
        "review": f"Заявка #{request_id} взята администратором в работу.",
        "approve": f"Заявка #{request_id} одобрена. Передача голды будет выполнена вручную.",
        "reject": f"Заявка #{request_id} отклонена.",
        "complete": f"Заявка #{request_id} отмечена выполненной администратором.",
    }
    try:
        await bot.send_message(request.user_id, user_messages[action])
    except TelegramAPIError:
        pass
    await callback.message.answer(
        f"Заявка #{request_id}: статус {request.status}. "
        "Для статуса «выполнена» сначала вручную передайте голду через игровой рынок.",
        reply_markup=_inline([[("Открыть заявку", f"adm:withdrawal:{request_id}")], [("← Заявки", "adm:withdrawals")]]),
    )
