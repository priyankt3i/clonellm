from __future__ import annotations
import json
import logging
from typing import Optional, Self
import uuid

from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain.chains.history_aware_retriever import create_history_aware_retriever
from langchain.chains.retrieval import create_retrieval_chain
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableSequence
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.vectorstores import VectorStoreRetriever
from langchain_community.chat_models import ChatLiteLLM
from langchain_community.vectorstores import Chroma
from langchain.text_splitter import CharacterTextSplitter, TextSplitter

from ._base import LiteLLMMixin
from ._prompt import context_prompt, user_profile_prompt, history_prompt, contextualize_question_prompt, question_prompt
from ._typing import UserProfile
from .embed import LiteLLMEmbeddings
from .memory import get_session_history, clear_session_history

logging.getLogger("langchain_core").setLevel(logging.ERROR)

__all__ = ("CloneLLM",)


class CloneLLM(LiteLLMMixin):
    """Creates an LLM clone of a user based on provided user profile and related context.

    Args:
        model (str): The name of the language model to use for text completion.
        documents (list[Document]): List of documents related to cloning user to use as context for the language model.
        embedding (LiteLLMEmbeddings | Embeddings): The embedding function to use for RAG.
        user_profile (Optional[UserProfile | dict | str]): The profile of the user to be cloned by the language model. Defaults to None.
        memory (Optional[bool]): Whether to enable the conversation memory (history). Defaults to None for no memory.
        api_key (Optional[str]): The API key to use. Defaults to None.
        **kwargs: Additional keyword arguments supported by the `langchain_community.chat_models.ChatLiteLLM` class.

    """

    __is_fitted: bool = False
    _splitter: TextSplitter = None
    _session_id: str = None
    db: Chroma = None

    _CHROMA_COLLECTION_NAME = "clonellm"

    def __init__(
        self,
        model: str,
        documents: list[Document],
        embedding: LiteLLMEmbeddings | Embeddings,
        user_profile: Optional[UserProfile | dict | str] = None,
        memory: Optional[bool] = None,
        api_key: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(model, api_key, **kwargs)
        self.embedding = embedding
        self.documents = documents
        self.user_profile = user_profile
        self.memory = memory
        self._internal_init()

    def _internal_init(self):
        api_key_kwarg = {f"{self._llm_provider}_api_key": self.api_key}
        self._llm = ChatLiteLLM(model=self.model, model_name=self.model, **api_key_kwarg, **self._litellm_kwargs)
        self._splitter = CharacterTextSplitter(chunk_size=2000, chunk_overlap=100)
        self.clear_memory()

    @classmethod
    def from_persist_directory(
        cls,
        persist_directory: str,
        model: str,
        embedding: LiteLLMEmbeddings | Embeddings,
        user_profile: Optional[UserProfile | dict | str] = None,
        memory: Optional[bool] = None,
        api_key: Optional[str] = None,
        **kwargs,
    ):
        cls.db = Chroma(
            collection_name=cls._CHROMA_COLLECTION_NAME, embedding_function=embedding, persist_directory=persist_directory
        )
        cls.__is_fitted = True
        return cls(
            model=model, documents=None, embedding=embedding, user_profile=user_profile, memory=memory, api_key=api_key, **kwargs
        )

    def _check_documents(self, documents: Optional[list[Document]] = None):
        for i, doc in enumerate(documents or self.documents):
            if not isinstance(doc, Document):
                raise ValueError(f"document at index {i} is not a valid Document")

    def fit(self) -> Self:
        self._check_documents()
        documents = self._splitter.split_documents(self.documents)
        self.db = Chroma.from_documents(documents, self.embedding, collection_name=self._CHROMA_COLLECTION_NAME)
        self.__is_fitted = True
        return self

    async def afit(self) -> Self:
        self._check_documents()
        documents = self._splitter.split_documents(self.documents)
        self.db = await Chroma.afrom_documents(documents, self.embedding, collection_name=self._CHROMA_COLLECTION_NAME)
        self.__is_fitted = True
        return self

    def _check_is_fitted(self, from_update: bool = False):
        if not self.__is_fitted or self.db is None or ((self._splitter is None) if from_update else False):
            raise AttributeError("This CloneLLM instance is not fitted yet. Call `fit` using this method.")

    def update(
        self,
        documents: list[Document],
    ) -> Self:
        self._check_is_fitted(from_update=True)
        self._check_documents(documents)
        documents = self._splitter.split_documents(self.documents)
        self.db.add_documents(documents)
        return self

    async def aupdate(
        self,
        documents: list[Document],
    ) -> Self:
        self._check_is_fitted(from_update=True)
        self._check_documents(documents)
        documents = self._splitter.split_documents(self.documents)
        await self.db.aadd_documents(documents)
        return self

    @property
    def _user_profile(self) -> str:
        if isinstance(self.user_profile, UserProfile):
            return self.user_profile.model_dump_json()
        elif isinstance(self.user_profile, dict):
            return json.dumps(self.user_profile)
        return str(self.user_profile)

    def _get_retriever(self) -> VectorStoreRetriever:
        return self.db.as_retriever(search_kwargs={"k": 1})

    def _get_rag_chain(self) -> RunnableSequence:
        prompt = context_prompt.copy()
        if self.user_profile:
            prompt += user_profile_prompt.format_messages(user_profile=self._user_profile)
        prompt += question_prompt
        return {"context": self._get_retriever(), "input": RunnablePassthrough()} | prompt | self._llm | StrOutputParser()

    def _get_rag_chain_with_history(self) -> RunnableWithMessageHistory:
        contextualize_system_prompt = contextualize_question_prompt + history_prompt + question_prompt
        history_aware_retriever = create_history_aware_retriever(self._llm, self._get_retriever(), contextualize_system_prompt)

        prompt = context_prompt
        if self.user_profile:
            prompt += user_profile_prompt.format_messages(user_profile=self._user_profile)
        prompt += history_prompt
        prompt += question_prompt
        question_answer_chain = create_stuff_documents_chain(self._llm, prompt)

        rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)
        return RunnableWithMessageHistory(
            rag_chain,
            get_session_history,
            input_messages_key="input",
            history_messages_key="chat_history",
            output_messages_key="answer",
            output_parser=StrOutputParser(),
        )

    def completion(self, prompt: str) -> str:
        self._check_is_fitted()
        if self.memory:
            rag_chain = self._get_rag_chain_with_history()
            return rag_chain.invoke({"input": prompt}, config={"configurable": {"session_id": self._session_id}})["answer"]
        rag_chain = self._get_rag_chain()
        return rag_chain.invoke(prompt)

    async def acompletion(self, prompt: str) -> str:
        self._check_is_fitted()
        if self.memory:
            rag_chain = self._get_rag_chain_with_history()
            response = await rag_chain.ainvoke({"input": prompt}, config={"configurable": {"session_id": self._session_id}})
            return response["answer"]
        rag_chain = self._get_rag_chain()
        return await rag_chain.ainvoke(prompt)

    def clear_memory(self):
        clear_session_history(self._session_id)
        self._session_id = str(uuid.uuid4())

    def __repr__(self) -> str:
        return f"CloneLLM<(model='{self.model}', memory='{self.memory}')>"
