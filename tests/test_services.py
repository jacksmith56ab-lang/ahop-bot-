import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import func, select

from database import init_database, make_engine, make_session_factory
from models import (
    DailyBonus,
    Giveaway,
    GiveawayParticipant,
    Order,
    Payment,
    Product,
    Transaction,
    User,
    utcnow,
)
from services.store import (
    claim_daily_bonus,
    create_order,
    draw_giveaway,
    initialize_settings,
    is_admin,
    record_star_payment,
    register_user,
    review_withdrawal,
)


def run_test(coro):
    asyncio.run(coro)


def test_admin_access_is_exact_match():
    assert is_admin(12345, 12345)
    assert not is_admin(12346, 12345)
    assert not is_admin(None, 12345)


def test_daily_bonus_allows_one_claim_per_configured_period():
    async def scenario():
        with TemporaryDirectory() as directory:
            engine = make_engine(Path(directory) / "test.sqlite3")
            await init_database(engine)
            sessions = make_session_factory(engine)
            await initialize_settings(sessions)
            async with sessions() as session, session.begin():
                session.add(User(telegram_id=1001, first_name="Test"))
            first_at = utcnow()
            first = await claim_daily_bonus(sessions, 1001, first_at)
            second = await claim_daily_bonus(sessions, 1001, first_at + timedelta(hours=23))
            third = await claim_daily_bonus(sessions, 1001, first_at + timedelta(hours=24))
            assert first.ok
            assert not second.ok
            assert second.remaining_seconds > 0
            assert third.ok
            async with sessions() as session:
                user = await session.get(User, 1001)
                claims = await session.scalar(
                    select(func.count()).select_from(DailyBonus).where(DailyBonus.user_id == 1001)
                )
                ledger_entries = await session.scalar(
                    select(func.count()).select_from(Transaction).where(
                        Transaction.user_id == 1001,
                        Transaction.kind == "daily_bonus",
                    )
                )
                assert claims == 2
                assert ledger_entries == 2
                assert user.balance == first.amount + third.amount
            await engine.dispose()

    run_test(scenario())


def test_star_payment_is_confirmed_once_and_order_waits_for_manual_delivery():
    async def scenario():
        with TemporaryDirectory() as directory:
            engine = make_engine(Path(directory) / "test.sqlite3")
            await init_database(engine)
            sessions = make_session_factory(engine)
            await initialize_settings(sessions)
            async with sessions() as session, session.begin():
                session.add(User(telegram_id=1002, first_name="Buyer"))
                product = Product(name="100 Gold", gold_amount=100, price_stars=12, is_active=True)
                session.add(product)
                await session.flush()
                product_id = product.id
            order = await create_order(sessions, 1002, product_id)
            assert order is not None
            applied, error = await record_star_payment(
                sessions, order.id, 1002, "telegram-charge-unique", 12, "XTR"
            )
            duplicate, duplicate_error = await record_star_payment(
                sessions, order.id, 1002, "telegram-charge-unique", 12, "XTR"
            )
            assert applied and not error
            assert duplicate and duplicate_error == "already_processed"
            async with sessions() as session:
                saved = await session.get(Order, order.id)
                payment_count = await session.scalar(select(func.count()).select_from(Payment))
                assert saved.status == "paid_pending_fulfillment"
                assert payment_count == 1
            await engine.dispose()

    run_test(scenario())


def test_giveaway_draw_has_no_duplicate_winners_and_caps_to_participants():
    async def scenario():
        with TemporaryDirectory() as directory:
            engine = make_engine(Path(directory) / "test.sqlite3")
            await init_database(engine)
            sessions = make_session_factory(engine)
            async with sessions() as session, session.begin():
                session.add_all(
                    [
                        User(telegram_id=2001, first_name="A"),
                        User(telegram_id=2002, first_name="B"),
                    ]
                )
                giveaway = Giveaway(
                    title="Test",
                    prize=10,
                    winners_count=5,
                    ends_at=utcnow() + timedelta(hours=1),
                    status="active",
                )
                session.add(giveaway)
                await session.flush()
                giveaway_id = giveaway.id
                session.add_all(
                    [
                        GiveawayParticipant(giveaway_id=giveaway_id, user_id=2001),
                        GiveawayParticipant(giveaway_id=giveaway_id, user_id=2002),
                    ]
                )
            created, winners, error = await draw_giveaway(sessions, giveaway_id)
            assert created and not error
            assert len(winners) == 2
            assert len(set(winners)) == 2
            async with sessions() as session:
                flags = list(
                    await session.scalars(
                        select(GiveawayParticipant).where(
                            GiveawayParticipant.giveaway_id == giveaway_id,
                            GiveawayParticipant.is_winner.is_(True),
                        )
                    )
                )
                assert len(flags) == len(winners)
            await engine.dispose()

    run_test(scenario())


def test_referral_reward_is_given_only_on_first_start():
    async def scenario():
        with TemporaryDirectory() as directory:
            engine = make_engine(Path(directory) / "test.sqlite3")
            await init_database(engine)
            sessions = make_session_factory(engine)
            await initialize_settings(sessions)
            async with sessions() as session, session.begin():
                session.add(User(telegram_id=3001, first_name="Referrer"))
            referred = SimpleNamespace(id=3002, username="new_user", first_name="New")
            first, is_new, reward = await register_user(sessions, referred, 3001)
            again, is_new_again, reward_again = await register_user(sessions, referred, 3001)
            async with sessions() as session:
                referrer = await session.get(User, 3001)
                ledger_count = await session.scalar(
                    select(func.count()).select_from(Transaction).where(
                        Transaction.user_id == 3001,
                        Transaction.kind == "referral",
                    )
                )
                assert referrer.balance == 10
                assert ledger_count == 1
            assert is_new and reward == 10
            assert not is_new_again and reward_again == 0
            await engine.dispose()

    run_test(scenario())


def test_withdrawal_deducts_once_when_approved_and_does_not_deduct_again_when_completed():
    async def scenario():
        with TemporaryDirectory() as directory:
            engine = make_engine(Path(directory) / "test.sqlite3")
            await init_database(engine)
            sessions = make_session_factory(engine)
            async with sessions() as session, session.begin():
                session.add(User(telegram_id=4001, first_name="Seller", balance=100))
                from models import Withdrawal

                request = Withdrawal(
                    user_id=4001,
                    amount=40,
                    lot_details="test lot",
                    status="new",
                )
                session.add(request)
                await session.flush()
                request_id = request.id
            approved, _, error = await review_withdrawal(sessions, request_id, "approve", 999)
            completed, _, completed_error = await review_withdrawal(sessions, request_id, "complete", 999)
            repeated, _, repeated_error = await review_withdrawal(sessions, request_id, "approve", 999)
            assert approved and not error
            assert completed and not completed_error
            assert not repeated and repeated_error
            async with sessions() as session:
                user = await session.get(User, 4001)
                assert user.balance == 60
            await engine.dispose()

    run_test(scenario())


def test_routers_register_user_and_admin_handlers():
    from handlers.admin import make_admin_router
    from handlers.user import make_user_router

    user_router = make_user_router(object(), 555, "test_bot")
    admin_router = make_admin_router(object(), 555)
    assert user_router.message.handlers
    assert user_router.callback_query.handlers
    assert user_router.pre_checkout_query.handlers
    assert admin_router.message.handlers
    assert admin_router.callback_query.handlers
