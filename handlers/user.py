from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from keyboards import back_user, user_home
from models import (
    DailyBonus,
    Giveaway,
    GiveawayParticipant,
    Order,
    Product,
    Referral,
    Task,
    TaskCompletion,
    Transaction,
    User,
    utcnow,
)
from services.store import (
    claim_daily_bonus,
    cancel_unpaid_order,
    create_order,
    create_withdrawal,
    get_setting,
    join_giveaway,
    record_star_payment,
    register_user,
)
from states import UserStates
from utils.time import format_remaining


def make_user_router(session_factory, admin_id: int, bot_username: str) -> Router:
    router = Router(name="users")

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject, bot: Bot):
        if message.from_user is None:
            return
        referrer_id = None
        args = command.args or ""
        if args.startswith("ref_") and args[4:].isdigit():
            referrer_id = int(args[4:])
        user, is_new, referral_reward = await register_user(
            session_factory, message.from_user, referrer_id
        )
        if user.is_blocked:
            await message.answer("Доступ к боту ограничен.")
            return
        if is_new and referral_reward and referrer_id:
            try:
                await bot.send_message(
                    referrer_id, f"Новый участник присоединился по вашей ссылке. Начислено: {referral_reward} голды."
                )
            except Exception:
                pass
        await message.answer(
            "Добро пожаловать в магазин голды Standoff 2.\n\n"
            "Покупки оплачиваются Telegram Stars. Передача голды в игре выполняется вручную "
            "администратором через рынок; бот не запрашивает пароль от игрового аккаунта.",
            reply_markup=user_home(),
        )

    @router.callback_query(F.data.startswith("u:"))
    async def user_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer()
        if callback.from_user is None or callback.message is None:
            return
        async with session_factory() as session:
            user = await session.get(User, callback.from_user.id)
            if not user:
                await callback.message.answer("Нажмите /start, чтобы создать профиль.")
                return
            if user.is_blocked:
                await callback.message.answer("Доступ к боту ограничен.")
                return

        action = callback.data or ""
        if action == "u:home":
            await callback.message.answer("Главное меню:", reply_markup=user_home())
            return

        if action == "u:daily":
            async with session_factory() as session:
                latest = await session.scalar(
                    select(DailyBonus)
                    .where(DailyBonus.user_id == callback.from_user.id)
                    .order_by(DailyBonus.claimed_at.desc())
                    .limit(1)
                )
                current = await session.get(User, callback.from_user.id)
                period_hours = int(await get_setting(session, "daily_period_hours", "24"))
            text = f"Баланс: {current.balance} голды.\n"
            if latest:
                wait = max(
                    0,
                    period_hours * 3600 - int((utcnow() - latest.claimed_at).total_seconds()),
                )
                if wait:
                    text += f"Следующий бонус станет доступен примерно через {format_remaining(wait)}."
                else:
                    text += "Ежедневный бонус доступен."
            else:
                text += "Ежедневный бонус доступен."
            await callback.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Получить бонус", callback_data="u:claim")],
                        [InlineKeyboardButton(text="← Главное меню", callback_data="u:home")],
                    ]
                ),
            )
            return

        if action in {"u:claim", "u:open"}:
            if action == "u:open":
                await bot.send_dice(chat_id=callback.from_user.id, emoji="🎲")
            result = await claim_daily_bonus(session_factory, callback.from_user.id)
            if result.ok:
                await callback.message.answer(
                    f"Выпало {result.amount} голды. Текущий баланс: {result.balance} голды.",
                    reply_markup=user_home(),
                )
            else:
                remaining = result.remaining_seconds
                wait_text = (
                    f"\nОсталось примерно {remaining // 3600} ч. {(remaining % 3600) // 60} мин."
                    if remaining
                    else ""
                )
                await callback.message.answer(result.message + wait_text, reply_markup=user_home())
            return

        if action == "u:balance":
            async with session_factory() as session:
                user = await session.get(User, callback.from_user.id)
            await callback.message.answer(f"Ваш баланс: {user.balance} голды.", reply_markup=user_home())
            return

        if action == "u:store":
            async with session_factory() as session:
                products = list(
                    await session.scalars(
                        select(Product).where(Product.is_active.is_(True)).order_by(Product.gold_amount)
                    )
                )
            if not products:
                await callback.message.answer("Пакеты пока не добавлены.", reply_markup=user_home())
                return
            for product in products:
                label = f"{product.name} — {product.gold_amount} голды — {product.price_stars} ⭐"
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Выбрать", callback_data=f"u:buy:{product.id}")]
                    ]
                )
                text = f"{label}\n{product.description}".strip()
                if product.photo_file_id:
                    await callback.message.answer_photo(product.photo_file_id, caption=text, reply_markup=kb)
                else:
                    await callback.message.answer(text, reply_markup=kb)
            await callback.message.answer("Оплата цифрового товара доступна только в Telegram Stars.", reply_markup=back_user())
            return

        if action.startswith("u:buy:"):
            product_id = int(action.rsplit(":", 1)[1])
            async with session_factory() as session:
                product = await session.get(Product, product_id)
            if not product or not product.is_active:
                await callback.message.answer("Этот пакет больше недоступен.", reply_markup=user_home())
                return
            await callback.message.answer(
                f"{product.name}\nКоличество: {product.gold_amount} голды\n"
                f"Цена: {product.price_stars} Telegram Stars\n\n"
                "После оплаты заказ появится у администратора для ручной передачи через игровой рынок.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Оплатить Stars", callback_data=f"u:checkout:{product.id}")],
                        [InlineKeyboardButton(text="← Пакеты", callback_data="u:store")],
                    ]
                ),
            )
            return

        if action.startswith("u:checkout:"):
            product_id = int(action.rsplit(":", 1)[1])
            order = await create_order(session_factory, callback.from_user.id, product_id)
            if not order:
                await callback.message.answer("Пакет недоступен. Обновите каталог.", reply_markup=user_home())
                return
            try:
                await bot.send_invoice(
                    chat_id=callback.from_user.id,
                    title=order.product_name[:32],
                    description=f"{order.gold_amount} голды Standoff 2. Передача выполняется вручную после оплаты.",
                    payload=f"order:{order.id}",
                    provider_token="",
                    currency="XTR",
                    prices=[LabeledPrice(label=f"{order.gold_amount} голды", amount=order.price)],
                )
            except Exception:
                await cancel_unpaid_order(session_factory, order.id, callback.from_user.id)
                await callback.message.answer(
                    "Не удалось создать счёт Stars. Попробуйте позже или напишите в поддержку.",
                    reply_markup=user_home(),
                )
            return

        if action == "u:giveaways":
            async with session_factory() as session:
                giveaways = list(
                    await session.scalars(
                        select(Giveaway)
                        .where(Giveaway.status == "active", Giveaway.ends_at > utcnow())
                        .order_by(Giveaway.ends_at)
                    )
                )
            if not giveaways:
                await callback.message.answer("Активных розыгрышей сейчас нет.", reply_markup=user_home())
                return
            for giveaway in giveaways:
                count = await _participant_count(session_factory, giveaway.id)
                text = (
                    f"🏆 {giveaway.title}\n{giveaway.description}\n"
                    f"Приз: {giveaway.prize} голды · Победителей: {giveaway.winners_count}\n"
                    f"Завершение (UTC): {giveaway.ends_at:%Y-%m-%d %H:%M} · Участников: {count}"
                )
                await callback.message.answer(
                    text,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Участвовать", callback_data=f"u:join:{giveaway.id}")]
                        ]
                    ),
                )
            return

        if action.startswith("u:join:"):
            giveaway_id = int(action.rsplit(":", 1)[1])
            ok, message = await join_giveaway(session_factory, giveaway_id, callback.from_user.id)
            await callback.message.answer(message, reply_markup=user_home())
            return

        if action == "u:orders":
            async with session_factory() as session:
                orders = list(
                    await session.scalars(
                        select(Order)
                        .where(Order.user_id == callback.from_user.id)
                        .order_by(Order.created_at.desc())
                        .limit(15)
                    )
                )
            if not orders:
                await callback.message.answer("Заказов пока нет.", reply_markup=user_home())
                return
            labels = {
                "awaiting_payment": "ожидает оплаты",
                "paid_pending_fulfillment": "оплачен, ожидает выдачи",
                "completed": "выполнен",
                "cancelled": "отменён",
                "refunded": "возврат",
            }
            text = "\n".join(
                f"#{order.id} · {order.product_name} · {order.gold_amount} голды · "
                f"{order.price} ⭐ · {labels.get(order.status, order.status)}"
                for order in orders
            )
            await callback.message.answer(text, reply_markup=user_home())
            return

        if action == "u:withdraw":
            await state.set_state(UserStates.withdrawal_amount)
            await callback.message.answer(
                "Укажите целое количество голды для заявки. Передача выполняется вручную после проверки администратором."
            )
            return

        if action == "u:tasks":
            async with session_factory() as session:
                tasks = list(await session.scalars(select(Task).where(Task.is_active.is_(True)).order_by(Task.id)))
                completions = {
                    row.task_id: row.status
                    for row in await session.scalars(
                        select(TaskCompletion).where(TaskCompletion.user_id == callback.from_user.id)
                    )
                }
            if not tasks:
                await callback.message.answer("Доступных заданий пока нет.", reply_markup=user_home())
                return
            for task in tasks:
                status = completions.get(task.id)
                label = {
                    "pending": "Ожидает проверки",
                    "completed": "Выполнено",
                    "rejected": "Отклонено",
                }.get(status, task.button_text or task.title)
                if status == "completed":
                    kb = None
                else:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text=label, callback_data=f"u:task:{task.id}")]
                        ]
                    )
                description = task.description.strip() if task.description else ""
                if description:
                    description = description.replace("{channel}", task.channel or "").replace("{reward}", str(task.reward))
                else:
                    description = f"Тип: {task.task_type}"
                    if task.channel:
                        description += f" · Канал: {task.channel}"
                    description += f"\nНаграда: {task.reward} голды"
                await callback.message.answer(
                    f"{task.title}\n{description}",
                    reply_markup=kb,
                )
            return

        if action.startswith("u:task:"):
            task_id = int(action.rsplit(":", 1)[1])
            async with session_factory() as session:
                task = await session.get(Task, task_id)
                existing = await session.scalar(
                    select(TaskCompletion).where(
                        TaskCompletion.task_id == task_id,
                        TaskCompletion.user_id == callback.from_user.id,
                    )
                )
            if not task or not task.is_active:
                await callback.message.answer("Задание недоступно.", reply_markup=user_home())
                return
            if existing:
                await callback.message.answer(
                    f"Статус задания: {existing.status}.", reply_markup=user_home()
                )
                return
            if task.task_type == "channel" and task.channel:
                try:
                    member = await bot.get_chat_member(task.channel, callback.from_user.id)
                    if member.status not in {
                        ChatMemberStatus.MEMBER,
                        ChatMemberStatus.ADMINISTRATOR,
                        ChatMemberStatus.CREATOR,
                    }:
                        await callback.message.answer(
                            f"Сначала подпишитесь на {task.channel}, затем нажмите кнопку ещё раз."
                        )
                        return
                    async with session_factory() as session:
                        async with session.begin():
                            user = await session.get(User, callback.from_user.id)
                            session.add(
                                TaskCompletion(task_id=task_id, user_id=callback.from_user.id, status="completed")
                            )
                            user.balance += task.reward
                            session.add(
                                Transaction(
                                    user_id=user.telegram_id,
                                    amount=task.reward,
                                    kind="task_reward",
                                    reason=f"Награда за задание «{task.title}»",
                                    reference=f"task:{task_id}:{user.telegram_id}",
                                )
                            )
                    await callback.message.answer(
                        f"Подписка подтверждена. Начислено {task.reward} голды.", reply_markup=user_home()
                    )
                except IntegrityError:
                    await callback.message.answer("Задание уже учтено.", reply_markup=user_home())
                except Exception:
                    await callback.message.answer(
                        "Не удалось проверить подписку автоматически. Проверьте, что бот имеет право "
                        "проверять участников канала, или отправьте подтверждение в поддержку.",
                        reply_markup=user_home(),
                    )
                return
            await state.set_state(UserStates.task_proof)
            await state.update_data(task_id=task_id)
            await callback.message.answer(
                "Опишите выполнение задания или отправьте подтверждение. Администратор проверит его вручную."
            )
            return

        if action == "u:referral":
            async with session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(Referral)
                    .where(Referral.referrer_id == callback.from_user.id)
                )
                reward = int(await get_setting(session, "referral_reward", "10"))
            link = f"https://t.me/{bot_username}?start=ref_{callback.from_user.id}"
            await callback.message.answer(
                f"Ваша ссылка:\n{link}\nПриглашено пользователей: {count or 0}\n"
                f"Награда за нового пользователя: {reward} голды.",
                reply_markup=user_home(),
            )
            return

        if action == "u:history":
            async with session_factory() as session:
                transactions = list(
                    await session.scalars(
                        select(Transaction)
                        .where(Transaction.user_id == callback.from_user.id)
                        .order_by(Transaction.created_at.desc())
                        .limit(20)
                    )
                )
            if not transactions:
                await callback.message.answer("История операций пока пуста.", reply_markup=user_home())
                return
            text = "\n".join(
                f"{row.created_at:%Y-%m-%d %H:%M} UTC · {row.amount:+} голды · {row.reason}"
                for row in transactions
            )
            await callback.message.answer(text[:4000], reply_markup=user_home())
            return

        if action == "u:support":
            await state.set_state(UserStates.support_message)
            await callback.message.answer("Напишите сообщение для поддержки. Мы передадим его администратору.")
            return

    @router.pre_checkout_query()
    async def pre_checkout(query, bot: Bot):
        valid = False
        try:
            payload = query.invoice_payload
            order_id = payload.removeprefix("order:")
            async with session_factory() as session:
                order = await session.get(Order, order_id)
                valid = bool(
                    order
                    and order.user_id == query.from_user.id
                    and order.status == "awaiting_payment"
                    and query.currency == "XTR"
                    and query.total_amount == order.price
                )
            await bot.answer_pre_checkout_query(
                query.id,
                ok=valid,
                error_message=None if valid else "Счёт недействителен или уже использован. Создайте новый заказ.",
            )
        except Exception:
            await bot.answer_pre_checkout_query(
                query.id, ok=False, error_message="Не удалось проверить заказ. Создайте новый счёт."
            )

    @router.message(F.successful_payment)
    async def successful_payment(message: Message, bot: Bot):
        payment = message.successful_payment
        if not payment or message.from_user is None:
            return
        order_id = payment.invoice_payload.removeprefix("order:")
        ok, error = await record_star_payment(
            session_factory,
            order_id,
            message.from_user.id,
            payment.telegram_payment_charge_id,
            payment.total_amount,
            payment.currency,
        )
        if ok and error == "already_processed":
            await message.answer(f"Оплата заказа #{order_id} уже учтена.")
            return
        if not ok:
            await message.answer(
                "Платёж получен, но заказ не удалось автоматически обновить. Сохраните номер "
                f"платежа и обратитесь в поддержку. Причина: {error}"
            )
            await bot.send_message(
                admin_id,
                f"Проверьте платёж вручную: user={message.from_user.id}, order={order_id}, "
                f"charge={payment.telegram_payment_charge_id}",
            )
            return
        async with session_factory() as session:
            order = await session.get(Order, order_id)
        await message.answer(
            f"Оплата получена. Заказ #{order_id} ожидает ручной выдачи голды администратором."
        )
        await bot.send_message(
            admin_id,
            f"Оплачен заказ #{order_id}: {order.gold_amount} голды, {payment.total_amount} ⭐. "
            f"Пользователь: {message.from_user.id}. Откройте /admin → Заказы.",
        )

    @router.message(UserStates.withdrawal_amount)
    async def withdrawal_amount(message: Message, state: FSMContext):
        if not message.from_user or not message.text or not message.text.strip().isdigit():
            await message.answer("Введите целое положительное число.")
            return
        amount = int(message.text.strip())
        if amount <= 0:
            await message.answer("Количество должно быть больше нуля.")
            return
        await state.update_data(withdrawal_amount=amount)
        await state.set_state(UserStates.withdrawal_details)
        await message.answer(
            "Отправьте параметры лота, необходимые администратору для ручной передачи через игровой рынок. "
            "Не отправляйте пароль от игрового аккаунта."
        )

    @router.message(UserStates.withdrawal_details)
    async def withdrawal_details(message: Message, state: FSMContext, bot: Bot):
        if not message.from_user or not message.text:
            await message.answer("Отправьте данные лота обычным текстом.")
            return
        data = await state.get_data()
        ok, request, error = await create_withdrawal(
            session_factory, message.from_user.id, int(data["withdrawal_amount"]), message.text
        )
        await state.clear()
        if not ok:
            await message.answer(error, reply_markup=user_home())
            return
        await message.answer(
            f"Заявка #{request.id} создана и ожидает проверки. Баланс спишется только после одобрения.",
            reply_markup=user_home(),
        )
        await bot.send_message(
            admin_id,
            f"Новая заявка #{request.id}: user={message.from_user.id}, {request.amount} голды.\n"
            f"Данные лота: {request.lot_details}",
        )

    @router.message(UserStates.support_message)
    async def support_message(message: Message, state: FSMContext, bot: Bot):
        if not message.from_user or not message.text:
            await message.answer("Отправьте сообщение обычным текстом.")
            return
        await bot.send_message(
            admin_id,
            f"Сообщение в поддержку от user={message.from_user.id} "
            f"(@{message.from_user.username or 'без username'}):\n{message.text[:3000]}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Ответить", callback_data=f"adm:supportreply:{message.from_user.id}")]
                ]
            ),
        )
        await state.clear()
        await message.answer("Сообщение передано администратору.", reply_markup=user_home())

    @router.message(UserStates.task_proof)
    async def task_proof(message: Message, state: FSMContext, bot: Bot):
        if not message.from_user:
            return
        data = await state.get_data()
        task_id = int(data.get("task_id", 0))
        proof = message.text or (message.caption or "") if message.photo else message.text
        if not proof:
            proof = "Фото подтверждения приложено."
        task_title = None
        async with session_factory() as session:
            async with session.begin():
                task = await session.get(Task, task_id)
                if not task or not task.is_active:
                    await state.clear()
                    await message.answer("Задание больше недоступно.", reply_markup=user_home())
                    return
                task_title = task.title
                existing = await session.scalar(
                    select(TaskCompletion).where(
                        TaskCompletion.task_id == task_id,
                        TaskCompletion.user_id == message.from_user.id,
                    )
                )
                if existing:
                    await state.clear()
                    await message.answer("Заявка уже существует.", reply_markup=user_home())
                    return
                session.add(
                    TaskCompletion(
                        task_id=task_id,
                        user_id=message.from_user.id,
                        status="pending",
                        proof=str(proof)[:2000],
                    )
                )
        if message.photo:
            await bot.send_photo(
                admin_id,
                message.photo[-1].file_id,
                caption=f"Подтверждение задания «{task_title}» #{task_id}, user={message.from_user.id}:\n{str(proof)[:800]}",
            )
        else:
            await bot.send_message(
                admin_id,
                f"Подтверждение задания «{task_title}» #{task_id}, user={message.from_user.id}:\n{str(proof)[:2000]}",
            )
        await state.clear()
        await message.answer("Подтверждение отправлено администратору.", reply_markup=user_home())

    @router.message(F.text.in_({"🎁 Бонус", "Бонус", "🛒 Купить голду", "Купить голду", "📁 Каталог", "Каталог", "📤 Вывести Gold", "Вывести Gold", "👤 Профиль", "Профиль", "ℹ️ Информация", "Информация"}))
    async def reply_menu_action(message: Message, state: FSMContext, bot: Bot):
        # Reuse the same action implementation as inline callbacks.
        from types import SimpleNamespace
        mapping = {
            "🎁 Бонус": "u:daily", "Бонус": "u:daily",
            "🛒 Купить голду": "u:store", "Купить голду": "u:store",
            "📁 Каталог": "u:store", "Каталог": "u:store",
            "📤 Вывести Gold": "u:withdraw", "Вывести Gold": "u:withdraw",
            "👤 Профиль": "u:balance", "Профиль": "u:balance",
            "ℹ️ Информация": "u:support", "Информация": "u:support",
        }
        class _CallbackMessage:
            def __init__(self, msg): self._msg = msg
            async def answer(self, *args, **kwargs): return await self._msg.answer(*args, **kwargs)
            async def answer_photo(self, *args, **kwargs): return await self._msg.answer_photo(*args, **kwargs)
            async def answer_document(self, *args, **kwargs): return await self._msg.answer_document(*args, **kwargs)
        async def noop(*args, **kwargs): return None
        fake = SimpleNamespace(
            answer=noop,
            from_user=message.from_user,
            message=_CallbackMessage(message),
            data=mapping[message.text],
        )
        await user_action(fake, state, bot)

    return router


async def _participant_count(session_factory, giveaway_id: int) -> int:
    from sqlalchemy import func

    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(GiveawayParticipant).where(
                    GiveawayParticipant.giveaway_id == giveaway_id
                )
            )
            or 0
        )
