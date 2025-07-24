import logging
from functools import lru_cache
from haystack import Pipeline
from haystack.components.builders import AnswerBuilder, PromptBuilder
from haystack.components.generators.openai import OpenAIGenerator
from haystack.components.embedders import SentenceTransformersTextEmbedder
from haystack.components.joiners import DocumentJoiner
from haystack.components.rankers.transformers_similarity import TransformersSimilarityRanker
from haystack.components.preprocessors import DocumentCleaner
from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore
from haystack_integrations.components.retrievers.elasticsearch import ElasticsearchBM25Retriever, ElasticsearchEmbeddingRetriever
from haystack.utils.auth import Secret
from app.core.config import *

logger = logging.getLogger(__name__)

# Connect to Elasticsearch
document_store = ElasticsearchDocumentStore(
    hosts=ELASTIC_HOST,
    http_auth=(ELASTIC_USER, ELASTIC_PASSWORD),
    verify_certs=False,
    index=ELASTIC_INDEX
)

@lru_cache(maxsize=len(AVAILABLE_MODELS))
def get_pipeline(model_name: str) -> Pipeline:
    """Create and return a cached RAG pipeline for the specified model."""
    logger.info(f"Creating or retrieving pipeline for model: {model_name}")
    if model_name not in AVAILABLE_MODELS:
        logger.warning(f"Model '{model_name}' not in AVAILABLE_MODELS. Defaulting to {AVAILABLE_MODELS[0]}.")
        model_name = AVAILABLE_MODELS[0]

    # Retrievers
    bm25_retriever = ElasticsearchBM25Retriever(
        document_store=document_store,
        top_k=8,
        fuzziness="AUTO:4,7"
    )

    text_embedder = SentenceTransformersTextEmbedder(
        model="paraphrase-multilingual-mpnet-base-v2",
        normalize_embeddings=True
    )

    embedding_retriever = ElasticsearchEmbeddingRetriever(
        document_store=document_store,
        top_k=6
    )

    # Document processing
    document_joiner = DocumentJoiner(
        join_mode="reciprocal_rank_fusion",
        weights=[1.0, 1.2]
    )

    document_cleaner = DocumentCleaner(
        remove_empty_lines=True,
        remove_extra_whitespaces=True
    )

    reranker = TransformersSimilarityRanker(
        model="cross-encoder/ms-marco-MiniLM-L-12-v2",
        top_k=5,
        batch_size=16
    )

    # LLM components
    template = """
    Je bent manager op een kindercentrum. Beantwoord vragen UITSLUITEND met informatie uit deze documenten:
    {% for doc in documents %}
    [Document {{loop.index}}: {{doc.meta.get('source', 'onbekend')}}]
    {{doc.content}}
    {% endfor %}

    Vraag: {{ query }}

    Antwoord (vermeld altijd bronnen):
    """
    prompt_builder = PromptBuilder(template)

    if model_name == "OpenAI":
        generator = OpenAIGenerator(
            model="gpt-4o",
            api_base_url=OPENAI_API_URL,
            generation_kwargs={
                "temperature": 0.1,
                "max_tokens": MAX_TOKENS
            }
        )
    else:
        # This if for the test.
        # generator = OpenAIGenerator(
        #     model="llama-3.3-70b-versatile",
        #     api_base_url="https://api.groq.com/openai/v1",
        #     api_key=Secret.from_token(os.getenv("GROQ_API_KEY")),
        #     generation_kwargs={"temperature": 0, "max_tokens": 1024},
        # )

        generator = OpenAIGenerator(
            model=model_name,
            api_base_url=LLM_API_URL,
            generation_kwargs={
                "temperature": 0,
                "max_tokens": MAX_TOKENS # Use configured max_tokens
            }
        )

    answer_builder = AnswerBuilder()

    # Build pipeline
    pipeline = Pipeline()
    pipeline.add_component("bm25_retriever", bm25_retriever)
    pipeline.add_component("text_embedder", text_embedder)
    pipeline.add_component("embedding_retriever", embedding_retriever)
    pipeline.add_component("document_joiner", document_joiner)
    pipeline.add_component("document_cleaner", document_cleaner)
    pipeline.add_component("reranker", reranker)
    pipeline.add_component("prompt_builder", prompt_builder)
    pipeline.add_component("llm", generator)
    pipeline.add_component("answer_builder", answer_builder)

    # Connect components
    pipeline.connect("text_embedder.embedding", "embedding_retriever.query_embedding")
    pipeline.connect("bm25_retriever.documents", "document_joiner.documents")
    pipeline.connect("embedding_retriever.documents", "document_joiner.documents")
    pipeline.connect("document_joiner.documents", "document_cleaner.documents")
    pipeline.connect("document_cleaner.documents", "reranker.documents")
    pipeline.connect("reranker.documents", "prompt_builder.documents")
    pipeline.connect("reranker.documents", "answer_builder.documents")
    pipeline.connect("prompt_builder.prompt", "llm.prompt")
    pipeline.connect("llm.replies", "answer_builder.replies")

    logger.info(f"Pipeline for model {model_name} created successfully.")
    return pipeline

def run_search(question: str, model: str = AVAILABLE_MODELS[0]) -> dict:
    """Run the pipeline and return answer with documents."""
    logger.info(f"Searching: '{question}' using model: {model}")
    
    if model not in AVAILABLE_MODELS:
        model = AVAILABLE_MODELS[0]
        logger.warning(f"Model not available, defaulting to {model}")

    try:
        pipeline = get_pipeline(model)
        res = pipeline.run({
            "bm25_retriever": {"query": question},
            "text_embedder": {"text": question},
            "prompt_builder": {"query": question},
            "reranker": {"query": question},
            "answer_builder": {"query": question}
        })

        # Get structured answer with documents
        answer_obj = res["answer_builder"]["answers"][0]
        
        return {
            "answer": answer_obj.data,
            "model_used": model,
            "documents": [
                {
                    "content": doc.content,
                    "meta": doc.meta,
                    "score": doc.score if hasattr(doc, "score") else None
                } 
                for doc in answer_obj.documents
            ]
        }

    except Exception as e:
        logger.error(f"Error processing question '{question}': {str(e)}", exc_info=True)
        raise ValueError(f"Pipeline error: {str(e)}")
