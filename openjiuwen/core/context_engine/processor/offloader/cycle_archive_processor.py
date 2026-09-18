# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Archive the minimum oldest complete cycles, without summarizing originals."""
# pylint: disable=protected-access  # Internal SessionModelContext integration.

from pydantic import BaseModel, ConfigDict

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError, build_error
from openjiuwen.core.context_engine.base import ContextWindow, ModelContext
from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.context_engine.context.react_cycle import removable_cycles
from openjiuwen.core.context_engine.context_engine import ContextEngine
from openjiuwen.core.context_engine.processor.base import ContextEvent, ContextProcessor
from openjiuwen.core.context_engine.processor.budget_guard import history_input_budget, history_window_tokens
from openjiuwen.core.context_engine.schema.messages import OffloadMixin, create_offload_message
from openjiuwen.core.foundation.llm import BaseMessage, ToolMessage


class CycleArchiveProcessorConfig(BaseModel):
    """Budget/storage come from ContextEngineConfig; no parallel threshold policy."""

    model_config = ConfigDict(extra="forbid")


@ContextEngine.register_processor()
class CycleArchiveProcessor(ContextProcessor):
    """Strict write-before-remove processor, used only with Session history enabled."""

    def save_state(self):
        """State belongs to the Session recorder, not a shared processor instance."""
        return {}

    def load_state(self, state):
        """No processor-local state to restore."""

    async def trigger_get_context_window(self, context, context_window, **kwargs) -> bool:
        return True

    async def on_get_context_window(self, context: ModelContext, context_window: ContextWindow, **kwargs):
        recorder = context._session_history
        try:
            if recorder.failure is not None:
                raise recorder.failure
            return self._archive(context, context_window)
        except BaseError as error:
            recorder.failure = error
            raise
        except Exception as error:
            recorder.failure = build_error(
                StatusCode.CONTEXT_ARCHIVE_EXECUTION_ERROR,
                error_msg=str(error),
                cause=error,
            )
            raise recorder.failure from error

    @staticmethod
    def _reference(context, messages: list[BaseMessage], *, tool: ToolMessage | None = None):
        recorder = context._session_history
        records = recorder.originals(messages)
        path = recorder.store.offload_path(records)
        fields = tool.model_dump() if tool is not None else {}
        for key in ("role", "content", "offload_handle", "offload_type"):
            fields.pop(key, None)
        placeholder = create_offload_message(
            role="tool" if tool is not None else "assistant",
            content=f"[[OFFLOAD: type=filesystem, path={path}]]",
            offload_handle=path.stem,
            offload_type="filesystem",
            **fields,
        )
        ContextUtils.ensure_context_message_ids([placeholder])
        return records, placeholder

    def _archive(self, context, window):  # pylint: disable=too-many-locals
        budget = history_input_budget(context)
        if history_window_tokens(context, window) <= budget:
            return None, window
        originals = list(window.context_messages)
        candidate = list(originals)
        removed: set[int] = set()
        writes = []
        recorder = context._session_history
        for indexes in removable_cycles(originals, recorder.protected_user_ids()):
            removed.update(indexes)
            records, placeholder = self._reference(context, [originals[i] for i in sorted(removed)])
            # One cumulative reference prevents a growing list of per-cycle hints.
            candidate = [placeholder] + [message for i, message in enumerate(originals) if i not in removed]
            writes = [(records, placeholder)]
            if history_window_tokens(context, window.model_copy(update={"context_messages": candidate})) <= budget:
                break
        if history_window_tokens(context, window.model_copy(update={"context_messages": candidate})) > budget:
            # Last-cycle tool results remain paired. Only replace a whole result
            # if its file reference is smaller; never cut a character prefix.
            for index, message in enumerate(candidate):
                if not isinstance(message, ToolMessage) or isinstance(message, OffloadMixin):
                    continue
                records, placeholder = self._reference(context, [message], tool=message)
                trial = candidate[:index] + [placeholder] + candidate[index + 1 :]
                trial_window = window.model_copy(update={"context_messages": trial})
                if history_window_tokens(context, trial_window) < history_window_tokens(
                    context,
                    window.model_copy(update={"context_messages": candidate}),
                ):
                    candidate = trial
                    writes.append((records, placeholder))
                if history_window_tokens(context, trial_window) <= budget:
                    break
        if not writes:
            return None, window  # The final request guard reports protected-content overflow.
        # Publish every file before changing either active state or outbound messages.
        references = [recorder.store.write_offload(records) for records, _ in writes]
        for ref in references:
            recorder.references[ref.archive_id] = ref
        context.set_messages(candidate)
        window.context_messages = candidate
        return ContextEvent(event_type="cycle_archive", messages_to_modify=sorted(removed)), window
