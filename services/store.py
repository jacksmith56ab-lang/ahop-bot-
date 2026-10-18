from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import SystemRandom
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

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


DEFAULT_SETTINGS = {
    "daily_enabled": "true",
    "daily_min": "5",
    "daily_max": "50",
    "daily_period_hours": "24",
    "referral_reward": "10",
    "crypto_pay_enabled": "false",
}

_user_locks: dict[int, asyncio.Lock] = {}
_order_locks: dict[str, asyncio.Lock] = {}


def _lock_for(locks: dict[Any, asyncio.Lock], key: Any) -> asyncio.Lock:
    return locks.setdefault(key, asyncio.Lock())


def is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


@dataclass
class BonusResult:
    ok: bool
    amount: int = 0
    balance: int = 0
    remaining_seconds: int = 0
    message: str = ""


async def initialize_settings(session_factory) -> None:
    async with session_factory() as session, session.begin():
        for key, value in DEFAULT_SETTINGS.items():
            if await session.get(Setting, key) is None:
                session.add(Setting(key=key, value=value))


async def get_setting(session, key: str, default: str | None = None) -> str:
    row = await session.get(Setting, key)
    if row is not None:
        return row.value
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key, "")


async def set_setting(session, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


async def register_user(session_factory, telegram_user, referrer_id: int | None = None) -> tuple[User, bool, int]:
    """Create/update a user and award a referral only on the referred user's first start."""
    async with session_factory() as session:
        async with session.begin():
            user = await session.get(User, telegram_user.id)
            is_new = user is None
            reward = 0
            if user is None:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=(telegram_user.first_name or "")[:128],
                )
                session.add(user)
                await session.flush()
                if referrer_id and referrer_id != telegram_user.id:
                    referrer = await session.get(User, referrer_id)
                    if referrer and not referrer.is_blocked:
                        reward = int(await get_setting(session, "referral_reward", "10"))
                        referral = Referral(
                            referrer_id=referrer_id,
                            referred_id=telegram_user.id,
                            reward=reward,
                            rewarded=True,
                        )
                        session.add(referral)
                        if reward > 0:
                            referrer.balance += reward
                            session.add(
                                Transaction(
                                    user_id=referrer_id,
                                    amount=reward,
                                    kind="referral",
                                    reason=f"Приглашён пользователь {telegram_user.id}",
                                    reference=f"referral:{telegram_user.id}",
                                )
                            )
            else:
                user.username = telegram_user.username
                user.first_name = (telegram_user.first_name or "")[:128]
                reward = 0
        return user, is_new, reward


async def claim_daily_bonus(session_factory, user_id: int, now: datetime | None = None) -> BonusResult:
    async with _lock_for(_user_locks, user_id):
        current_time = now or utcnow()
        async with session_factory() as session:
            async with session.begin():
                user = await session.get(User, user_id)
                if user is None:
                    return BonusResult(False, message="Сначала нажмите /start.")
                if user.is_blocked:
                    return BonusResult(False, message="Доступ к боту ограничен.")
                if (await get_setting(session, "daily_enabled", "true")).lower() != "true":
                    return BonusResult(False, message="Ежедневный бонус сейчас отключён.")

                hours = max(1, int(await get_setting(session, "daily_period_hours", "24")))
                last = await session.scalar(
                    select(DailyBonus)
                    .where(DailyBonus.user_id == user_id)
                    .order_by(DailyBonus.claimed_at.desc())
                    .limit(1)
                )
                if last is not None:
                    available_at = last.claimed_at + timedelta(hours=hours)
                    if current_time < available_at:
                        return BonusResult(
                            False,
                            balance=user.balance,
                            remaining_seconds=max(1, int((available_at - current_time).total_seconds())),
                            message="Бонус уже получен. Попробуйте позже.",
                        )

                low = max(0, int(await get_setting(session, "daily_min", "5")))
                high = max(low, int(await get_setting(session, "daily_max", "50")))
                amount = secrets.randbelow(high - low + 1) + low
                user.balance += amount
                session.add(DailyBonus(user_id=user_id, amount=amount, claimed_at=current_time))
                session.add(
                    Transaction(
                        user_id=user_id,
                        amount=amount,
                        kind="daily_bonus",
                        reason="Ежедневный бонус",
                        reference=f"daily:{user_id}:{current_time.isoformat()}",
                    )
                )
                return BonusResult(True, amount=amount, balance=user.balance)


async def change_balance(
    session_factory,
    user_id: int,
    amount: int,
    kind: str,
    reason: str,
    reference: str,
) -> tuple[bool, int, str]:
    async with _lock_for(_user_locks, user_id):
        async with session_factory() as session:
            async with session.begin():
                if await session.scalar(select(Transaction.id).where(Transaction.reference == reference)):
                    user = await session.get(User, user_id)
                    return False, user.balance if user else 0, "Эта операция уже была обработана."
                user = await session.get(User, user_id)
                if user is None:
                    return False, 0, "Пользователь не найден."
                if user.balance + amount < 0:
                    return False, user.balance, "Недостаточно средств на балансе."
                user.balance += amount
                session.add(
                    Transaction(
                        user_id=user_id,
                        amount=amount,
                        kind=kind,
                        reason=reason[:300],
                        reference=reference,
                    )
                )
                return True, user.balance, ""


async def create_order(session_factory, user_id: int, product_id: int) -> Order | None:
    async with session_factory() as session:
        async with session.begin():
            product = await session.get(Product, product_id)
            user = await session.get(User, user_id)
            if product is None or not product.is_active or user is None or user.is_blocked:
                return None
            order = Order(
                id=secrets.token_hex(8),
                user_id=user_id,
                product_id=product.id,
                product_name=product.name,
                gold_amount=product.gold_amount,
                price=product.price_stars,
                currency="XTR",
            )
            session.add(order)
            await session.flush()
            return order


async def cancel_unpaid_order(session_factory, order_id: str, user_id: int) -> None:
    async with session_factory() as session, session.begin():
        order = await session.get(Order, order_id)
        if order and order.user_id == user_id and order.status == "awaiting_payment":
            order.status = "cancelled"
            order.updated_at = utcnow()


async def record_star_payment(
    session_factory,
    order_id: str,
    user_id: int,
    charge_id: str,
    amount: int,
    currency: str,
) -> tuple[bool, str]:
    """Persist a successful Stars charge exactly once and move the order to fulfillment."""
    async with _lock_for(_order_locks, order_id):
        try:
            async with session_factory() as session:
                async with session.begin():
                    prior_charge = await session.scalar(
                        select(Payment.id).where(
                            Payment.provider == "telegram_stars",
                            Payment.charge_id == charge_id,
                        )
                    )
                    if prior_charge is not None:
                        return True, "already_processed"
                    order = await session.get(Order, order_id)
                    if order is None or order.user_id != user_id:
                        return False, "Заказ не найден."
                    if order.status != "awaiting_payment":
                        return False, "Заказ уже обработан или недоступен."
                    if currency != "XTR" or amount != order.price:
                        return False, "Сумма или валюта платежа не совпадает с заказом."
                    session.add(
                        Payment(
                            order_id=order.id,
                            user_id=user_id,
                            provider="telegram_stars",
                            charge_id=charge_id,
                            amount=amount,
                            currency=currency,
                            status="succeeded",
                        )
                    )
                    order.status = "paid_pending_fulfillment"
                    order.updated_at = utcnow()
            return True, ""
        except IntegrityError:
            return False, "Этот платёж уже обработан."


async def join_giveaway(session_factory, giveaway_id: int, user_id: int) -> tuple[bool, str]:
    async with session_factory() as session:
        async with session.begin():
            giveaway = await session.get(Giveaway, giveaway_id)
            user = await session.get(User, user_id)
            if not giveaway or giveaway.status != "active" or giveaway.ends_at <= utcnow():
                return False, "Этот розыгрыш уже завершён."
            if not user or user.is_blocked:
                return False, "Пользователь не найден или доступ ограничен."
            exists = await session.scalar(
                select(GiveawayParticipant.id).where(
                    GiveawayParticipant.giveaway_id == giveaway_id,
                    GiveawayParticipant.user_id == user_id,
                )
            )
            if exists:
                return False, "Вы уже участвуете в этом розыгрыше."
            session.add(GiveawayParticipant(giveaway_id=giveaway_id, user_id=user_id))
            return True, "Вы участвуете в розыгрыше."


async def draw_giveaway(session_factory, giveaway_id: int) -> tuple[bool, list[int], str]:
    async with session_factory() as session:
        async with session.begin():
            giveaway = await session.get(Giveaway, giveaway_id)
            if not giveaway:
                return False, [], "Розыгрыш не найден."
            if giveaway.status != "active":
                winners = list(
                    await session.scalars(
                        select(GiveawayParticipant.user_id).where(
                            GiveawayParticipant.giveaway_id == giveaway_id,
                            GiveawayParticipant.is_winner.is_(True),
                        )
                    )
                )
                return False, winners, "Розыгрыш уже завершён."
            participants = list(
                await session.scalars(
                    select(GiveawayParticipant).where(GiveawayParticipant.giveaway_id == giveaway_id)
                )
            )
            winners = SystemRandom().sample(participants, min(giveaway.winners_count, len(participants)))
            for participant in participants:
                participant.is_winner = participant in winners
            giveaway.status = "finished"
            giveaway.finished_at = utcnow()
            return True, [participant.user_id for participant in winners], ""


async def grant_giveaway_prize(
    session_factory, giveaway_id: int, user_id: int
) -> tuple[bool, int, str]:
    reference = f"giveaway:{giveaway_id}:{user_id}"
    async with _lock_for(_user_locks, user_id):
        async with session_factory() as session:
            async with session.begin():
                participant = await session.scalar(
                    select(GiveawayParticipant).where(
                        GiveawayParticipant.giveaway_id == giveaway_id,
                        GiveawayParticipant.user_id == user_id,
                    )
                )
                giveaway = await session.get(Giveaway, giveaway_id)
                user = await session.get(User, user_id)
                if not participant or not participant.is_winner or not giveaway or not user:
                    return False, user.balance if user else 0, "Победитель не найден."
                if participant.prize_delivered:
                    return False, user.balance, "Приз уже выдан."
                participant.prize_delivered = True
                user.balance += giveaway.prize
                session.add(
                    Transaction(
                        user_id=user_id,
                        amount=giveaway.prize,
                        kind="giveaway_prize",
                        reason=f"Приз розыгрыша «{giveaway.title}»",
                        reference=reference,
                    )
                )
                return True, user.balance, ""


async def create_withdrawal(
    session_factory, user_id: int, amount: int, lot_details: str
) -> tuple[bool, Withdrawal | None, str]:
    async with _lock_for(_user_locks, user_id):
        async with session_factory() as session:
            async with session.begin():
                user = await session.get(User, user_id)
                if not user or user.is_blocked:
                    return False, None, "Пользователь не найден или доступ ограничен."
                reserved = await session.scalar(
                    select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                        Withdrawal.user_id == user_id,
                        Withdrawal.status.in_(("new", "reviewing")),
                    )
                )
                if amount <= 0 or user.balance - int(reserved or 0) < amount:
                    return False, None, "Недостаточно свободного баланса."
                request = Withdrawal(
                    user_id=user_id,
                    amount=amount,
                    lot_details=lot_details[:1000],
                    status="new",
                )
                session.add(request)
                await session.flush()
                return True, request, ""


async def review_withdrawal(session_factory, withdrawal_id: int, action: str, admin_id: int):
    if action not in {"review", "approve", "reject", "complete"}:
        return False, None, "Недопустимое действие."
    async with session_factory() as session:
        async with session.begin():
            request = await session.get(Withdrawal, withdrawal_id)
            if not request:
                return False, None, "Заявка не найдена."
            user = await session.get(User, request.user_id)
            if action == "review" and request.status == "new":
                request.status = "reviewing"
            elif action == "approve" and request.status in {"new", "reviewing"}:
                if not user or user.balance < request.amount:
                    return False, request, "На балансе больше недостаточно средств."
                user.balance -= request.amount
                request.status = "approved"
                session.add(
                    Transaction(
                        user_id=request.user_id,
                        amount=-request.amount,
                        kind="withdrawal",
                        reason=f"Одобрена заявка #{request.id}",
                        reference=f"withdrawal:{request.id}:approved",
                    )
                )
            elif action == "reject" and request.status in {"new", "reviewing"}:
                request.status = "rejected"
            elif action == "reject" and request.status == "approved":
                if user:
                    user.balance += request.amount
                    session.add(
                        Transaction(
                            user_id=request.user_id,
                            amount=request.amount,
                            kind="withdrawal_refund",
                            reason=f"Возврат по отклонённой заявке #{request.id}",
                            reference=f"withdrawal:{request.id}:refund",
                        )
                    )
                request.status = "rejected"
            elif action == "complete" and request.status == "approved":
                request.status = "completed"
            else:
                return False, request, f"Нельзя выполнить действие для статуса {request.status}."
            request.updated_at = utcnow()
            session.add(
                AdminLog(
                    admin_id=admin_id,
                    action=f"withdrawal_{action}",
                    target_id=request.user_id,
                    details=f"Заявка #{request.id}, {request.amount} голды",
                )
            )
            return True, request, ""


async def approve_manual_task(
    session_factory, task_id: int, user_id: int, admin_id: int, approved: bool
) -> tuple[bool, str]:
    async with _lock_for(_user_locks, user_id):
        async with session_factory() as session:
            async with session.begin():
                completion = await session.scalar(
                    select(TaskCompletion).where(
                        TaskCompletion.task_id == task_id,
                        TaskCompletion.user_id == user_id,
                    )
                )
                task = await session.get(Task, task_id)
                user = await session.get(User, user_id)
                if not completion or completion.status != "pending" or not task or not user:
                    return False, "Заявка уже обработана или не найдена."
                if approved:
                    completion.status = "completed"
                    user.balance += task.reward
                    session.add(
                        Transaction(
                            user_id=user_id,
                            amount=task.reward,
                            kind="task_reward",
                            reason=f"Награда за задание «{task.title}»",
                            reference=f"task:{task_id}:{user_id}",
                        )
                    )
                else:
                    completion.status = "rejected"
                session.add(
                    AdminLog(
                        admin_id=admin_id,
                        action="task_approved" if approved else "task_rejected",
                        target_id=user_id,
                        details=f"Задание #{task_id}",
                    )
                )
                return True, ""
