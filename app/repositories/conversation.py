from app.models.conversation import Conversation
from app.repositories.base import TenantScopedRepository


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation
