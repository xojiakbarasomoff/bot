from app.models.user import User
from app.repositories.base import TenantScopedRepository


class UserRepository(TenantScopedRepository[User]):
    model = User
