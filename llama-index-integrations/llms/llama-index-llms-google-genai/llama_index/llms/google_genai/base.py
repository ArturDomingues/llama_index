"""Google's hosted Gemini API."""

import functools
import os
import typing
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Generator,
    List,
    Optional,
    Sequence,
    Type,
    Union,
    Callable,
)


import llama_index.core.instrumentation as instrument
from llama_index.core.base.llms.generic_utils import (
    chat_to_completion_decorator,
    achat_to_completion_decorator,
    stream_chat_to_completion_decorator,
    astream_chat_to_completion_decorator,
)
from llama_index.core.base.llms.types import (
    ChatMessage,
    ChatResponse,
    ChatResponseAsyncGen,
    ChatResponseGen,
    CompletionResponse,
    CompletionResponseAsyncGen,
    CompletionResponseGen,
    LLMMetadata,
    MessageRole,
)
from llama_index.core.bridge.pydantic import BaseModel, Field, PrivateAttr
from llama_index.core.callbacks import CallbackManager
from llama_index.core.constants import DEFAULT_TEMPERATURE, DEFAULT_NUM_OUTPUTS
from llama_index.core.llms.callbacks import llm_chat_callback, llm_completion_callback
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.llms.llm import ToolSelection, Model
from llama_index.core.prompts import PromptTemplate
from llama_index.core.program.utils import FlexibleModel, create_flexible_model
from llama_index.core.types import PydanticProgramMode
from llama_index.llms.google_genai.utils import (
    chat_from_gemini_response,
    chat_message_to_gemini,
    convert_schema_to_function_declaration,
    prepare_chat_params,
    handle_streaming_flexible_model,
    create_retry_decorator,
)

import google.genai
import google.auth
import google.genai.types as types

dispatcher = instrument.get_dispatcher(__name__)

DEFAULT_MODEL = "gemini-2.0-flash"

if TYPE_CHECKING:
    from llama_index.core.tools.types import BaseTool


class VertexAIConfig(typing.TypedDict):
    credentials: Optional[google.auth.credentials.Credentials] = None
    project: Optional[str] = None
    location: Optional[str] = None


def llm_retry_decorator(f: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(f)
    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        max_retries = getattr(self, "max_retries", 0)
        if max_retries <= 0:
            return f(self, *args, **kwargs)

        retry = create_retry_decorator(
            max_retries=max_retries,
            random_exponential=True,
            stop_after_delay_seconds=60,
            min_seconds=1,
            max_seconds=20,
        )
        return retry(f)(self, *args, **kwargs)

    return wrapper


class GoogleGenAI(FunctionCallingLLM):
    """
    Google GenAI LLM.

    Examples:
        `pip install llama-index-llms-google-genai`

        ```python
        from llama_index.llms.google_genai import GoogleGenAI

        llm = GoogleGenAI(model="gemini-2.0-flash", api_key="YOUR_API_KEY")
        resp = llm.complete("Write a poem about a magic backpack")
        print(resp)
        ```

    """

    model: str = Field(default=DEFAULT_MODEL, description="The Gemini model to use.")
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE,
        description="The temperature to use during generation.",
        ge=0.0,
        le=2.0,
    )
    context_window: Optional[int] = Field(
        default=None,
        description="The context window of the model. If not provided, the default context window 200000 will be used.",
    )
    max_retries: int = Field(
        default=3,
        description="The maximum number of API retries.",
        ge=0,
    )
    is_function_calling_model: bool = Field(
        default=True, description="Whether the model is a function calling model."
    )
    cached_content: Optional[str] = Field(
        default=None,
        description="Cached content to use for the model.",
    )
    built_in_tool: Optional[types.Tool] = Field(
        default=None,
        description="Google GenAI tool to use for the model to augment responses.",
    )

    _max_tokens: int = PrivateAttr()
    _client: google.genai.Client = PrivateAttr()
    _generation_config: types.GenerateContentConfigDict = PrivateAttr()
    _model_meta: types.Model = PrivateAttr()

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        max_retries: int = 3,
        vertexai_config: Optional[VertexAIConfig] = None,
        http_options: Optional[types.HttpOptions] = None,
        debug_config: Optional[google.genai.client.DebugConfig] = None,
        generation_config: Optional[types.GenerateContentConfig] = None,
        callback_manager: Optional[CallbackManager] = None,
        is_function_calling_model: bool = True,
        cached_content: Optional[str] = None,
        built_in_tool: Optional[types.Tool] = None,
        **kwargs: Any,
    ):
        # API keys are optional. The API can be authorised via OAuth (detected
        # environmentally) or by the GOOGLE_API_KEY environment variable.
        api_key = api_key or os.getenv("GOOGLE_API_KEY", None)
        vertexai = (
            vertexai_config is not None
            or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false") != "false"
        )
        project = (vertexai_config or {}).get("project") or os.getenv(
            "GOOGLE_CLOUD_PROJECT", None
        )
        location = (vertexai_config or {}).get("location") or os.getenv(
            "GOOGLE_CLOUD_LOCATION", None
        )

        config_params: Dict[str, Any] = {
            "api_key": api_key,
        }

        if vertexai_config is not None:
            config_params.update(vertexai_config)
            config_params["api_key"] = None
            config_params["vertexai"] = True
        elif vertexai:
            config_params["project"] = project
            config_params["location"] = location
            config_params["api_key"] = None
            config_params["vertexai"] = True

        if http_options:
            config_params["http_options"] = http_options

        if debug_config:
            config_params["debug_config"] = debug_config

        client = google.genai.Client(**config_params)
        model_meta = client.models.get(model=model)

        super().__init__(
            model=model,
            temperature=temperature,
            context_window=context_window,
            callback_manager=callback_manager,
            is_function_calling_model=is_function_calling_model,
            max_retries=max_retries,
            cached_content=cached_content,
            built_in_tool=built_in_tool,
            **kwargs,
        )

        self.model = model
        self._client = client
        self._model_meta = model_meta
        # store this as a dict and not as a pydantic model so we can more easily
        # merge it later
        if generation_config:
            self._generation_config = generation_config.model_dump()
            if cached_content:
                self._generation_config.setdefault("cached_content", cached_content)
            if built_in_tool is not None:
                if self._generation_config.get("tools") is None:
                    self._generation_config["tools"] = []
                if isinstance(self._generation_config["tools"], list):
                    if len(self._generation_config["tools"]) > 0:
                        raise ValueError(
                            "Providing multiple Google GenAI tools or mixing with custom tools is not supported."
                        )
                self._generation_config["tools"].append(built_in_tool)
        else:
            config_kwargs = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "cached_content": cached_content,
            }
            if built_in_tool:
                config_kwargs["tools"] = [built_in_tool]

            self._generation_config = types.GenerateContentConfig(
                **config_kwargs
            ).model_dump()
        self._max_tokens = (
            max_tokens or model_meta.output_token_limit or DEFAULT_NUM_OUTPUTS
        )

    @classmethod
    def class_name(cls) -> str:
        return "GenAI"

    @property
    def metadata(self) -> LLMMetadata:
        if self.context_window is None:
            base = self._model_meta.input_token_limit or 200000
            total_tokens = base + self._max_tokens
        else:
            total_tokens = self.context_window

        return LLMMetadata(
            context_window=total_tokens,
            num_output=self._max_tokens,
            model_name=self.model,
            is_chat_model=True,
            is_function_calling_model=self.is_function_calling_model,
        )

    @llm_completion_callback()
    def complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        chat_fn = chat_to_completion_decorator(self._chat)
        return chat_fn(prompt, **kwargs)

    @llm_completion_callback()
    async def acomplete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        chat_fn = achat_to_completion_decorator(self._achat)
        return await chat_fn(prompt, **kwargs)

    @llm_completion_callback()
    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        chat_fn = stream_chat_to_completion_decorator(self._stream_chat)
        return chat_fn(prompt, **kwargs)

    @llm_completion_callback()
    async def astream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseAsyncGen:
        chat_fn = astream_chat_to_completion_decorator(self.astream_chat)
        return await chat_fn(prompt, **kwargs)

    @llm_retry_decorator
    def _chat(self, messages: Sequence[ChatMessage], **kwargs: Any):
        generation_config = {
            **(self._generation_config or {}),
            **kwargs.pop("generation_config", {}),
        }
        params = {**kwargs, "generation_config": generation_config}
        next_msg, chat_kwargs = prepare_chat_params(self.model, messages, **params)
        chat = self._client.chats.create(**chat_kwargs)
        response = chat.send_message(next_msg.parts)
        return chat_from_gemini_response(response)

    @llm_retry_decorator
    async def _achat(self, messages: Sequence[ChatMessage], **kwargs: Any):
        generation_config = {
            **(self._generation_config or {}),
            **kwargs.pop("generation_config", {}),
        }
        params = {**kwargs, "generation_config": generation_config}
        next_msg, chat_kwargs = prepare_chat_params(self.model, messages, **params)
        chat = self._client.aio.chats.create(**chat_kwargs)
        response = await chat.send_message(next_msg.parts)
        return chat_from_gemini_response(response)

    @llm_chat_callback()
    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        return self._chat(messages, **kwargs)

    @llm_chat_callback()
    async def achat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponse:
        return await self._achat(messages, **kwargs)

    def _stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseGen:
        generation_config = {
            **(self._generation_config or {}),
            **kwargs.pop("generation_config", {}),
        }
        params = {**kwargs, "generation_config": generation_config}
        next_msg, chat_kwargs = prepare_chat_params(self.model, messages, **params)
        chat = self._client.chats.create(**chat_kwargs)
        response = chat.send_message_stream(next_msg.parts)

        def gen() -> ChatResponseGen:
            content = ""
            existing_tool_calls = []
            thoughts = ""
            for r in response:
                if not r.candidates:
                    continue

                top_candidate = r.candidates[0]
                content_delta = top_candidate.content.parts[0].text
                if content_delta:
                    if top_candidate.content.parts[0].thought:
                        thoughts += content_delta
                    else:
                        content += content_delta
                llama_resp = chat_from_gemini_response(r)
                existing_tool_calls.extend(
                    llama_resp.message.additional_kwargs.get("tool_calls", [])
                )
                llama_resp.delta = content_delta
                llama_resp.message.content = content
                llama_resp.message.additional_kwargs["thoughts"] = thoughts
                llama_resp.message.additional_kwargs["tool_calls"] = existing_tool_calls
                yield llama_resp

        return gen()

    @llm_chat_callback()
    def stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseGen:
        return self._stream_chat(messages, **kwargs)

    async def _astream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseAsyncGen:
        generation_config = {
            **(self._generation_config or {}),
            **kwargs.pop("generation_config", {}),
        }
        params = {**kwargs, "generation_config": generation_config}
        next_msg, chat_kwargs = prepare_chat_params(self.model, messages, **params)
        chat = self._client.aio.chats.create(**chat_kwargs)

        async def gen() -> ChatResponseAsyncGen:
            content = ""
            existing_tool_calls = []
            thoughts = ""
            async for r in await chat.send_message_stream(next_msg.parts):
                if candidates := r.candidates:
                    if not candidates:
                        continue

                    top_candidate = candidates[0]
                    if response_content := top_candidate.content:
                        if parts := response_content.parts:
                            content_delta = parts[0].text
                            if content_delta:
                                if parts[0].thought:
                                    thoughts += content_delta
                                else:
                                    content += content_delta
                            llama_resp = chat_from_gemini_response(r)
                            existing_tool_calls.extend(
                                llama_resp.message.additional_kwargs.get(
                                    "tool_calls", []
                                )
                            )
                            llama_resp.delta = content_delta
                            llama_resp.message.content = content
                            llama_resp.message.additional_kwargs["thoughts"] = thoughts
                            llama_resp.message.additional_kwargs["tool_calls"] = (
                                existing_tool_calls
                            )
                            yield llama_resp

        return gen()

    @llm_chat_callback()
    async def astream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseAsyncGen:
        return await self._astream_chat(messages, **kwargs)

    def _prepare_chat_with_tools(
        self,
        tools: Sequence["BaseTool"],
        user_msg: Optional[Union[str, ChatMessage]] = None,
        chat_history: Optional[List[ChatMessage]] = None,
        verbose: bool = False,
        allow_parallel_tool_calls: bool = False,
        tool_required: bool = False,
        tool_choice: Optional[Union[str, dict]] = None,
        strict: Optional[bool] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Predict and call the tool."""
        if tool_choice is None:
            tool_choice = "any" if tool_required else "auto"

        if tool_choice == "auto":
            tool_mode = types.FunctionCallingConfigMode.AUTO
        elif tool_choice == "none":
            tool_mode = types.FunctionCallingConfigMode.NONE
        else:
            tool_mode = types.FunctionCallingConfigMode.ANY

        function_calling_config = types.FunctionCallingConfig(mode=tool_mode)

        if tool_choice not in ["auto", "none"]:
            if isinstance(tool_choice, dict):
                raise ValueError("Gemini does not support tool_choice as a dict")

            # assume that the user wants a tool call to be made
            # if the tool choice is not in the list of tools, then we will make a tool call to all tools
            # otherwise, we will make a tool call to the tool choice
            tool_names = [tool.metadata.name for tool in tools if tool.metadata.name]
            if tool_choice not in tool_names:
                function_calling_config.allowed_function_names = tool_names
            else:
                function_calling_config.allowed_function_names = [tool_choice]

        tool_config = types.ToolConfig(
            function_calling_config=function_calling_config,
        )

        tool_declarations = []
        for tool in tools:
            if tool.metadata.fn_schema:
                function_declaration = convert_schema_to_function_declaration(
                    self._client, tool
                )
                tool_declarations.append(function_declaration)

        if isinstance(user_msg, str):
            user_msg = ChatMessage(role=MessageRole.USER, content=user_msg)

        messages = chat_history or []
        if user_msg:
            messages.append(user_msg)

        return {
            "messages": messages,
            "tools": (
                [types.Tool(function_declarations=tool_declarations)]
                if tool_declarations
                else None
            ),
            "tool_config": tool_config,
            **kwargs,
        }

    def get_tool_calls_from_response(
        self,
        response: ChatResponse,
        error_on_no_tool_call: bool = True,
        **kwargs: Any,
    ) -> List[ToolSelection]:
        """Predict and call the tool."""
        tool_calls = response.message.additional_kwargs.get("tool_calls", [])

        if len(tool_calls) < 1:
            if error_on_no_tool_call:
                raise ValueError(
                    f"Expected at least one tool call, but got {len(tool_calls)} tool calls."
                )
            else:
                return []

        tool_selections = []
        for tool_call in tool_calls:
            tool_selections.append(
                ToolSelection(
                    tool_id=tool_call["name"],
                    tool_name=tool_call["name"],
                    tool_kwargs=tool_call["args"],
                )
            )

        return tool_selections

    @dispatcher.span
    def structured_predict_without_function_calling(
        self,
        output_cls: Type[Model],
        prompt: PromptTemplate,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        **prompt_args: Any,
    ) -> Model:
        """Structured predict."""
        llm_kwargs = llm_kwargs or {}

        messages = prompt.format_messages(**prompt_args)
        response = self._client.models.generate_content(
            model=self.model,
            contents=list(map(chat_message_to_gemini, messages)),
            **{
                **llm_kwargs,
                **{
                    "config": {
                        "response_mime_type": "application/json",
                        "response_schema": output_cls,
                    }
                },
            },
        )

        if isinstance(response.parsed, BaseModel):
            return response.parsed
        else:
            raise ValueError("Response is not a BaseModel")

    @dispatcher.span
    def structured_predict(
        self,
        output_cls: Type[Model],
        prompt: PromptTemplate,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        **prompt_args: Any,
    ) -> Model:
        """Structured predict."""
        llm_kwargs = llm_kwargs or {}

        if self.pydantic_program_mode == PydanticProgramMode.DEFAULT:
            generation_config = {
                **(self._generation_config or {}),
                **llm_kwargs.pop("generation_config", {}),
            }

            # set the specific types needed for the response
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = output_cls

            messages = prompt.format_messages(**prompt_args)
            contents = list(map(chat_message_to_gemini, messages))
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=generation_config,
            )

            if isinstance(response.parsed, BaseModel):
                return response.parsed
            else:
                raise ValueError("Response is not a BaseModel")

        else:
            return super().structured_predict(
                output_cls, prompt, llm_kwargs=llm_kwargs, **prompt_args
            )

    @dispatcher.span
    async def astructured_predict(
        self,
        output_cls: Type[Model],
        prompt: PromptTemplate,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        **prompt_args: Any,
    ) -> Model:
        """Structured predict."""
        llm_kwargs = llm_kwargs or {}

        if self.pydantic_program_mode == PydanticProgramMode.DEFAULT:
            generation_config = {
                **(self._generation_config or {}),
                **llm_kwargs.pop("generation_config", {}),
            }

            # set the specific types needed for the response
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = output_cls

            messages = prompt.format_messages(**prompt_args)
            contents = list(map(chat_message_to_gemini, messages))
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=generation_config,
            )

            if isinstance(response.parsed, BaseModel):
                return response.parsed
            else:
                raise ValueError("Response is not a BaseModel")

        else:
            return super().structured_predict(
                output_cls, prompt, llm_kwargs=llm_kwargs, **prompt_args
            )

    @dispatcher.span
    def stream_structured_predict(
        self,
        output_cls: Type[Model],
        prompt: PromptTemplate,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        **prompt_args: Any,
    ) -> Generator[Union[Model, FlexibleModel], None, None]:
        """Stream structured predict."""
        llm_kwargs = llm_kwargs or {}

        if self.pydantic_program_mode == PydanticProgramMode.DEFAULT:
            generation_config = {
                **(self._generation_config or {}),
                **llm_kwargs.pop("generation_config", {}),
            }

            # set the specific types needed for the response
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = output_cls

            messages = prompt.format_messages(**prompt_args)
            contents = list(map(chat_message_to_gemini, messages))

            def gen() -> Generator[Union[Model, FlexibleModel], None, None]:
                flexible_model = create_flexible_model(output_cls)
                response_gen = self._client.models.generate_content_stream(
                    model=self.model,
                    contents=contents,
                    config=generation_config,
                )

                current_json = ""
                for chunk in response_gen:
                    if chunk.parsed:
                        yield chunk.parsed
                    elif chunk.candidates:
                        streaming_model, current_json = handle_streaming_flexible_model(
                            current_json,
                            chunk.candidates[0],
                            output_cls,
                            flexible_model,
                        )
                        if streaming_model:
                            yield streaming_model

            return gen()
        else:
            return super().stream_structured_predict(
                output_cls, prompt, llm_kwargs=llm_kwargs, **prompt_args
            )

    @dispatcher.span
    async def astream_structured_predict(
        self,
        output_cls: Type[Model],
        prompt: PromptTemplate,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        **prompt_args: Any,
    ) -> AsyncGenerator[Union[Model, FlexibleModel], None]:
        """Stream structured predict."""
        llm_kwargs = llm_kwargs or {}

        if self.pydantic_program_mode == PydanticProgramMode.DEFAULT:
            generation_config = {
                **(self._generation_config or {}),
                **llm_kwargs.pop("generation_config", {}),
            }

            # set the specific types needed for the response
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = output_cls

            messages = prompt.format_messages(**prompt_args)
            contents = list(map(chat_message_to_gemini, messages))

            async def gen() -> AsyncGenerator[Union[Model, FlexibleModel], None]:
                flexible_model = create_flexible_model(output_cls)
                response_gen = await self._client.aio.models.generate_content_stream(
                    model=self.model,
                    contents=contents,
                    config=generation_config,
                )

                current_json = ""
                async for chunk in response_gen:
                    if chunk.parsed:
                        yield chunk.parsed
                    elif chunk.candidates:
                        streaming_model, current_json = handle_streaming_flexible_model(
                            current_json,
                            chunk.candidates[0],
                            output_cls,
                            flexible_model,
                        )
                        if streaming_model:
                            yield streaming_model

            return gen()
        else:
            return await super().astream_structured_predict(
                output_cls, prompt, llm_kwargs=llm_kwargs, **prompt_args
            )
