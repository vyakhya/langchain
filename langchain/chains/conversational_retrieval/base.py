"""Chain for chatting with a vector database."""
from __future__ import annotations

import warnings
from abc import abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from pydantic import Extra, Field, root_validator

from langchain.base_language import BaseLanguageModel
from langchain.callbacks.manager import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
)
from langchain.chains.base import Chain
from langchain.chains.combine_documents.base import BaseCombineDocumentsChain
from langchain.chains.combine_documents.stuff import StuffDocumentsChain
from langchain.chains.conversational_retrieval.prompts import CONDENSE_QUESTION_PROMPT
from langchain.chains.llm import LLMChain
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts.base import BasePromptTemplate
from langchain.schema import BaseMessage, BaseRetriever, Document
from langchain.vectorstores.base import VectorStore

# Depending on the memory type and configuration, the chat history format may differ.
# This needs to be consolidated.
CHAT_TURN_TYPE = Union[Tuple[str, str], BaseMessage]


_ROLE_MAP = {"human": "Human: ", "ai": "Assistant: "}


def _get_chat_history(chat_history: List[CHAT_TURN_TYPE]) -> str:
    buffer = ""
    for dialogue_turn in chat_history:
        if isinstance(dialogue_turn, BaseMessage):
            role_prefix = _ROLE_MAP.get(dialogue_turn.type, f"{dialogue_turn.type}: ")
            buffer += f"\n{role_prefix}{dialogue_turn.content}"
        elif isinstance(dialogue_turn, tuple):
            human = "Human: " + dialogue_turn[0]
            ai = "Assistant: " + dialogue_turn[1]
            buffer += "\n" + "\n".join([human, ai])
        else:
            raise ValueError(
                f"Unsupported chat history format: {type(dialogue_turn)}."
                f" Full chat history: {chat_history} "
            )
    return buffer


class BaseConversationalRetrievalChain(Chain):
    """Chain for chatting with an index."""

    combine_docs_chain: BaseCombineDocumentsChain
    question_generator: LLMChain
    output_key: str = "answer"
    return_source_documents: bool = False
    get_chat_history: Optional[Callable[[CHAT_TURN_TYPE], str]] = None
    """Return the source documents."""

    class Config:
        """Configuration for this pydantic object."""

        # extra = Extra.forbid
        ## VS update
        extra = Extra.allow
        ## VS update ends
        arbitrary_types_allowed = True
        allow_population_by_field_name = True

    @property
    def input_keys(self) -> List[str]:
        """Input keys."""
        return ["question", "chat_history"]

    @property
    def output_keys(self) -> List[str]:
        """Return the output keys.

        :meta private:
        """
        _output_keys = [self.output_key]
        if self.return_source_documents:
            _output_keys = _output_keys + ["source_documents"]
        return _output_keys

    @abstractmethod
    def _get_docs(self, question: str, inputs: Dict[str, Any]) -> List[Document]:
        """Get docs."""

    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        _run_manager = run_manager or CallbackManagerForChainRun.get_noop_manager()
        question = inputs["question"]
        get_chat_history = self.get_chat_history or _get_chat_history
        chat_history_str = get_chat_history(inputs["chat_history"])

        if chat_history_str:
            callbacks = _run_manager.get_child()
            new_question = self.question_generator.run(
                question=question, chat_history=chat_history_str, callbacks=callbacks
            )
        else:
            new_question = question

        # VS: Updated code

        try:
            filter = inputs.get("search_kwargs", {})['filter']
        except Exception:
            filter = 'No filters applied'

        # Transform filter to simpler format for output
        if filter is None or filter == 'No filters applied':
            simplified_filter = 'No filters applied'
        else:
            simplified_filter = {}

            if '$and' in filter:
                conditions = filter['$and']
            else:
                conditions = [filter]

            for condition in conditions:
                # loop through the $or condition, if any
                if '$or' in condition:
                    for or_condition in condition['$or']:
                        for key, value in or_condition.items():
                            # if key is already in the result dictionary, append the value
                            if key in simplified_filter:
                                simplified_filter[key].append(value['$eq'])
                            else:
                                simplified_filter[key] = [value['$eq']]
                else:
                    # if no $or condition
                    for key, value in condition.items():
                        if key in simplified_filter:
                            simplified_filter[key].append(value['$eq'])
                        else:
                            simplified_filter[key] = [value['$eq']]

        relevant_docs, n_post_filters = self._get_docs(new_question, inputs)
        if n_post_filters == 0:
            result = {"answer": "No comments found for these attributes",
                      "filter": filter,
                      "simplified_filter": simplified_filter,
                      "n_comments": n_post_filters}
            return {self.output_key: result}
        if n_post_filters < 10:
            result = {"answer": "No. of comments in this group is less than the confidentiality threshold (10)."
                                "Please try broadening the group.",
                      "filter": filter,
                      "simplified_filter": simplified_filter,
                      "n_comments": n_post_filters}
            return {self.output_key: result}
        else:
            new_inputs = inputs.copy()
            new_inputs["question"] = new_question
            new_inputs["chat_history"] = chat_history_str
            answer, _ = self.combine_docs_chain.combine_docs(relevant_docs, **new_inputs)
            # answer = f"{answer} \n\n {filter} No. of comments in this group = {n_post_filters}"
            result = {
                "answer": answer,
                "filter": filter,
                "simplified_filter": simplified_filter,
                "n_comments": n_post_filters}
            if self.return_source_documents:
                return {self.output_key: result, "source_documents": relevant_docs}
            else:
                return {self.output_key: result}
        #VS: Updated code ends

        # docs = self._get_docs(new_question, inputs)
        # new_inputs = inputs.copy()
        # new_inputs["question"] = new_question
        # new_inputs["chat_history"] = chat_history_str
        # answer = self.combine_docs_chain.run(
        #     input_documents=docs, callbacks=_run_manager.get_child(), **new_inputs
        # )
        # if self.return_source_documents:
        #     return {self.output_key: answer, "source_documents": docs}
        # else:
        #     return {self.output_key: answer}

    @abstractmethod
    async def _aget_docs(self, question: str, inputs: Dict[str, Any]) -> List[Document]:
        """Get docs."""

    async def _acall(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[AsyncCallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        _run_manager = run_manager or AsyncCallbackManagerForChainRun.get_noop_manager()
        question = inputs["question"]
        get_chat_history = self.get_chat_history or _get_chat_history
        chat_history_str = get_chat_history(inputs["chat_history"])
        if chat_history_str:
            callbacks = _run_manager.get_child()
            new_question = await self.question_generator.arun(
                question=question, chat_history=chat_history_str, callbacks=callbacks
            )
        else:
            new_question = question
        docs = await self._aget_docs(new_question, inputs)
        new_inputs = inputs.copy()
        new_inputs["question"] = new_question
        new_inputs["chat_history"] = chat_history_str
        answer = await self.combine_docs_chain.arun(
            input_documents=docs, callbacks=_run_manager.get_child(), **new_inputs
        )
        if self.return_source_documents:
            return {self.output_key: answer, "source_documents": docs}
        else:
            return {self.output_key: answer}

    def save(self, file_path: Union[Path, str]) -> None:
        if self.get_chat_history:
            raise ValueError("Chain not savable when `get_chat_history` is not None.")
        super().save(file_path)


class ConversationalRetrievalChain(BaseConversationalRetrievalChain):
    """Chain for chatting with an index."""

    retriever: BaseRetriever
    """Index to connect to."""
    max_tokens_limit: Optional[int] = None
    """If set, restricts the docs to return from store based on tokens, enforced only
    for StuffDocumentChain"""

    def _reduce_tokens_below_limit(self, docs: List[Document]) -> List[Document]:
        num_docs = len(docs)

        if self.max_tokens_limit and isinstance(
            self.combine_docs_chain, StuffDocumentsChain
        ):
            tokens = [
                self.combine_docs_chain.llm_chain.llm.get_num_tokens(doc.page_content)
                for doc in docs
            ]
            token_count = sum(tokens[:num_docs])
            while token_count > self.max_tokens_limit:
                num_docs -= 1
                token_count -= tokens[num_docs]

        return docs[:num_docs]

    def _get_docs(self, question: str, inputs: Dict[str, Any]) -> List[Document]:
        docs = self.retriever.get_relevant_documents(question)
        return self._reduce_tokens_below_limit(docs)

    async def _aget_docs(self, question: str, inputs: Dict[str, Any]) -> List[Document]:
        docs = await self.retriever.aget_relevant_documents(question)
        return self._reduce_tokens_below_limit(docs)

    @classmethod
    def from_llm(
        cls,
        llm: BaseLanguageModel,
        retriever: BaseRetriever,
        condense_question_prompt: BasePromptTemplate = CONDENSE_QUESTION_PROMPT,
        chain_type: str = "stuff",
        verbose: bool = False,
        combine_docs_chain_kwargs: Optional[Dict] = None,
        **kwargs: Any,
    ) -> BaseConversationalRetrievalChain:
        """Load chain from LLM."""
        combine_docs_chain_kwargs = combine_docs_chain_kwargs or {}
        doc_chain = load_qa_chain(
            llm,
            chain_type=chain_type,
            verbose=verbose,
            **combine_docs_chain_kwargs,
        )
        condense_question_chain = LLMChain(
            llm=llm, prompt=condense_question_prompt, verbose=verbose
        )
        return cls(
            retriever=retriever,
            combine_docs_chain=doc_chain,
            question_generator=condense_question_chain,
            **kwargs,
        )


class ChatVectorDBChain(BaseConversationalRetrievalChain):
    """Chain for chatting with a vector database."""

    vectorstore: VectorStore = Field(alias="vectorstore")
    # top_k_docs_for_context: int = 4
    # VS Update:
    n_docs_pre_filter: int = 3490
    top_k_docs_for_context: int = 50
    # VS Update ends
    search_kwargs: dict = Field(default_factory=dict)

    @property
    def _chain_type(self) -> str:
        return "chat-vector-db"

    @root_validator()
    def raise_deprecation(cls, values: Dict) -> Dict:
        warnings.warn(
            "`ChatVectorDBChain` is deprecated - "
            "please use `from langchain.chains import ConversationalRetrievalChain`"
        )
        return values

    def _get_docs(self, question: str, inputs: Dict[str, Any]) -> List[Document]:
        vectordbkwargs = inputs.get("vectordbkwargs", {})
        # full_kwargs = {**self.search_kwargs, **vectordbkwargs}
        # return self.vectorstore.similarity_search(
        #     question, k=self.top_k_docs_for_context, **full_kwargs
        # )
        ## VS update code:
        search_kwargs = inputs.get("search_kwargs", {})
        full_kwargs = {**search_kwargs, **vectordbkwargs}
        documents = self.vectorstore.similarity_search(
            question, n=self.n_docs_pre_filter, k=self.top_k_docs_for_context, **full_kwargs
        )
        ## Old update - getting unique documents is not needed, earlier I had duplicates in chromadb due to running ingest_data.py twice
        # unique_documents = []
        # for doc in documents:
        #     if not any(d.page_content == doc.page_content and d.metadata == doc.metadata for d in unique_documents):
        #         unique_documents.append(doc)
        # print(len(documents))
        # print(len(unique_documents))
        # return unique_documents
        return documents
        ## VS Updated code ends ##

    async def _aget_docs(self, question: str, inputs: Dict[str, Any]) -> List[Document]:
        raise NotImplementedError("ChatVectorDBChain does not support async")

    @classmethod
    def from_llm(
        cls,
        llm: BaseLanguageModel,
        vectorstore: VectorStore,
        condense_question_prompt: BasePromptTemplate = CONDENSE_QUESTION_PROMPT,
        chain_type: str = "stuff",
        combine_docs_chain_kwargs: Optional[Dict] = None,
        **kwargs: Any,
    ) -> BaseConversationalRetrievalChain:
        """Load chain from LLM."""
        combine_docs_chain_kwargs = combine_docs_chain_kwargs or {}
        doc_chain = load_qa_chain(
            llm,
            chain_type=chain_type,
            **combine_docs_chain_kwargs,
        )
        condense_question_chain = LLMChain(llm=llm, prompt=condense_question_prompt)
        return cls(
            vectorstore=vectorstore,
            combine_docs_chain=doc_chain,
            question_generator=condense_question_chain,
            **kwargs,
        )
