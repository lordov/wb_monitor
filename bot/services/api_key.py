from cryptography.fernet import Fernet, InvalidToken

from bot.api.wb import WBAPIClient
from bot.database.models import ApiKey
from bot.database.uow import UnitOfWork
from bot.schemas.wb import ApiKeyWithTelegramDTO
from bot.services.subscription import SubscriptionService
from ..core.logging import app_logger


class ApiKeyDecryptionError(Exception):
    pass


class ApiKeyService:
    def __init__(self, uow: UnitOfWork, fernet: Fernet):
        self.uow = uow
        self.api_key = uow.api_keys
        self.users = uow.users
        self.employee = uow.employee
        self.task_status = uow.task_status
        self.fernet = fernet

    async def get_user_key(self, telegram_id: int) -> ApiKeyWithTelegramDTO:
        user = await self.users.get_by_tg_id(telegram_id)
        if not user:
            raise ValueError("User not found")
        app_logger.info("Fetching API key",
                        user_id=user.id)
        key = await self.api_key.get_active_by_user(user.id)
        return key

    async def get_decrypted_by_title(self, telegram_id: int, title: str) -> str | None:
        user = await self.users.get_by_tg_id(telegram_id)
        if not user:
            raise ValueError("User not found")

        app_logger.info("Getting and decrypting API key by title",
                        user_id=user.id, title=title)
        key = await self.api_key.get_by_title(user.id, title)
        if not key:
            app_logger.info("API key not found", user_id=user.id, title=title)
            return None
        return await self.decrypt_key(key.key_encrypted)

    async def get_all_decrypted_keys(self) -> list[ApiKeyWithTelegramDTO]:
        app_logger.info("Getting all decrypted API keys")
        try:
            # Получаем все ключи из репозитория
            keys = await self.api_key.get_all_active_keys()
        except Exception as e:
            app_logger.warning(f"Failed to fetch API keys: {e}")
            return []

        return keys

    async def add_encrypt_key(self, telegram_id: int, raw_key: str, title: str = "API Key") -> ApiKey:
        user = await self.users.get_by_tg_id(telegram_id)
        if not user:
            raise ValueError("User not found")
        app_logger.info("Encrypting and adding API key",
                        user_id=user.id, title=title)
        encrypted = self.fernet.encrypt(raw_key.encode()).decode()
        key = await self.api_key.add_one({
            "user_id": user.id,
            "title": title,
            "key_encrypted": encrypted,
        })
        app_logger.info("API key added successfully", key_id=key.id)
        return key

    async def decrypt_key(self, encrypted_key: str) -> str:
        app_logger.debug("Decrypting API key")
        try:
            return self.fernet.decrypt(encrypted_key.encode()).decode()
        except InvalidToken:
            app_logger.warning("Failed to decrypt API key: invalid token")
            raise ApiKeyDecryptionError("Invalid or corrupted key")

    async def set_key(self, user_id: int, title: str, raw_key: str, is_active: bool = True) -> None:
        encrypted = self.fernet.encrypt(raw_key.encode()).decode()
        await self.api_key.upsert_key(user_id, title, encrypted, is_active=is_active)

    async def delete_key(self, telegram_id: int):
        user = await self.users.get_by_tg_id(telegram_id)
        if not user:
            raise ValueError("User not found")
        await self.api_key.delete_user_keys(user.id)
        # Подумать над правильным удалением сотрудников
        await self.employee.delete_all_employees(user.id)
        await self.task_status.delete_all_tasks(user.id)

    async def validate_wb_api_key(self, key: str) -> bool:
        return len(key) > 30

    async def check_request_to_wb(self, raw_key: str) -> bool:
        """Проверяет валидность ключа через метод ping Wildberries."""
        client = WBAPIClient(plain_token=raw_key)

        try:
            response = await client.ping_wb()
            if response['Status'] == 'OK':
                return True
            return False
        except Exception as e:
            # Логировать можно здесь, если хочешь
            return False

    async def set_api_key_with_subscription_check(
        self,
        telegram_id: int,
        title: str,
        raw_key: str,
        subscription_service: SubscriptionService,
    ) -> tuple[bool, str]:
        """
        Сохраняет ключ API с учётом подписки.
        Возвращает:
        - is_active: bool — активен ли ключ.
        - status: str — один из: "active", "trial_activated", "inactive".
        """
        user = await self.users.get_by_tg_id(telegram_id)
        if not user:
            raise ValueError("User not found")
        # Проверка на активную подписку
        if await subscription_service.has_active_subscription(user.id):
            await self.set_key(user.id, title, raw_key, is_active=True)
            return "active"

        # Можно ли дать пробную подписку?
        if await subscription_service.check_trial(user.id):
            await subscription_service.create_subscription(user.id, plan="trial")
            await self.set_key(user.id, title, raw_key, is_active=True)
            return "trial_activated"

        # Иначе сохраняем неактивный ключ
        await self.set_key(user.id, title, raw_key, is_active=False)
        return "inactive"

    async def handle_unauthorized_key(self, user_id: int) -> bool:
        """
        Обрабатывает случай неактивного API ключа (401 ошибка).
        Деактивирует ключ в базе данных.

        Args:
            user_id: ID пользователя

        Returns:
            bool: True если ключ был деактивирован, False если не найден
        """
        try:
            # Деактивируем API ключ в базе данных
            deactivated = await self.api_key.deactivate_key_by_user_id(user_id)

            if deactivated:
                app_logger.info(f"API key deactivated for user {user_id}")
                return True

        except Exception as e:
            app_logger.error(
                f"Failed to handle unauthorized key for user {user_id}: {e}")
            raise
