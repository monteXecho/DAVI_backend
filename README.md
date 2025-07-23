# MijnDAVI RAG POC

© 2025 by MijnDAVI

## Overview

MijnDAVI RAG POC is a Retrieval-Augmented Generation (RAG) system designed specifically for childcare center management. The system allows managers and staff to query internal documents using natural language questions and receive accurate, contextual answers from their documentation corpus.

The project implements a modern RAG pipeline using Haystack 2.11+ framework with Elasticsearch as the vector database and supports both local and cloud-based language models.

## Features

- **Multi-Model Support**: Compatible with local models (Granite 3.2/3.3) and OpenAI GPT-4
- **Hybrid Search**: Combines BM25 and semantic search for improved document retrieval
- **Document Reranking**: Uses transformer-based reranking for better relevance
- **FastAPI REST API**: Production-ready API with automatic documentation
- **Streamlit Web Interface**: User-friendly chat interface for testing
- **Docker Support**: Easy deployment with Docker Compose
- **Multilingual Support**: Optimized for Dutch content with multilingual embeddings

## Architecture

### Core Components

1. **Document Indexing Pipeline** (`pipelines/indexing_pipeline.py`)
   - Processes PDF documents from the `documenten-import/` folder
   - Splits documents into chunks with overlap
   - Generates embeddings using multilingual sentence transformers
   - Stores in Elasticsearch with both text and vector search capabilities

2. **RAG API** (`app/main.py`)
   - FastAPI-based REST API
   - Hybrid retrieval (BM25 + semantic search)
   - Document reranking and cleaning
   - Caching pipeline per model for performance
   - Comprehensive error handling and logging

3. **Chat Interfaces**
   - `chat-api.py`: Alternative FastAPI implementation
   - `chat-local-llm-ranker.py`: Streamlit web interface

4. **Infrastructure**
   - Elasticsearch 8.11.1 for document storage and search
   - Docker Compose for local development
   - Support for both local LLM servers and OpenAI API

### Search Pipeline Flow

```
User Query → BM25 Retriever ────┐
             Text Embedder → Vector Retriever ────┐
                                                  ├→ Document Joiner → Document Cleaner → Reranker → Prompt Builder → LLM → Answer Builder
```

## Installation

### Prerequisites

- Python 3.8+
- Docker and Docker Compose
- At least 4GB RAM available for Elasticsearch

### Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd mijndavi-rag-poc
   ```

2. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start Elasticsearch**
   ```bash
   docker-compose up -d
   ```

4. **Configure environment variables**
   Create a `.env.local` file in the root directory:
   ```env
   ELASTIC_HOST=http://localhost:9200
   ELASTIC_USER=elastic
   ELASTIC_PASSWORD=elastic
   ELASTIC_INDEX=haystack_test
   LLM_API_URL=http://your-local-llm:1234/v1
   OPENAI_API_URL=https://api.openai.com/v1
   OPENAI_API_KEY=your-openai-key
   MAX_TOKENS=1024
   ```

5. **Index your documents**
   Place PDF files in the `documenten-import/` folder and run:
   ```bash
   python pipelines/indexing_pipeline.py
   ```

## Usage

### Running the API Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://localhost:8000` with automatic documentation at `http://localhost:8000/docs`.

### API Endpoints

#### POST `/ask`
Ask a question to the RAG system.

**Request Body:**
```json
{
  "question": "Hoe meld ik een incident?",
  "model": "granite-3.2-8b-instruct@q8_0"
}
```

**Response:**
```json
{
  "answer": "Om een incident te melden...",
  "model_used": "granite-3.2-8b-instruct@q8_0"
}
```

#### GET `/`
Health check endpoint.

### Using cURL

```bash
curl -X POST "http://localhost:8000/ask" \
  -H "Content-Type: application/json" \
  -d '{"question": "Hoe meld ik een incident?", "model": "granite-3.2-8b-instruct@q8_0"}'
```


## Available Models

The system supports multiple language models:

- `granite-3.2-8b-instruct@q8_0` (Local)
- `granite-3.2-8b-instruct@f16` (Local)
- `OpenAI` (GPT-4 via OpenAI API)

## Document Types

Currently supported document formats:
- PDF files (placed in `documenten-import/` folder)

Example documents included:
- Childcare policies and procedures
- Safety guidelines
- Employee handbooks
- Regulatory compliance documents

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ELASTIC_HOST` | `http://localhost:9200` | Elasticsearch host URL |
| `ELASTIC_USER` | `elastic` | Elasticsearch username |
| `ELASTIC_PASSWORD` | `elastic` | Elasticsearch password |
| `ELASTIC_INDEX` | `haystack_test` | Elasticsearch index name |
| `LLM_API_URL` | `http://localhost:1234/v1` | Local LLM server URL |
| `OPENAI_API_URL` | `https://api.openai.com/v1` | OpenAI API base URL |
| `MAX_TOKENS` | `1024` | Maximum tokens for LLM responses |

### Pipeline Optimization

The RAG pipeline is optimized for childcare documentation with:

- **BM25 Settings**: `top_k=8`, `fuzziness="AUTO:4,7"`
- **Vector Search**: `top_k=6` with normalized embeddings
- **Document Joining**: Reciprocal rank fusion with embedding weight of 1.2
- **Reranking**: Top 5 documents using cross-encoder model
- **Chunking**: 150 words with 50-word overlap

## Development

### Project Structure

```
mijndavi-rag-poc/
├── app/
│   ├── __init__.py
│   ├── main.py                                # FastAPI application entry point
│   ├── api/
│   │    ├── __init__.py
│   │    └── apk.py                            # Route logic (/ask)
│   ├── core/
│   │    ├── __init__.py
│   │    ├── pipeline.py                       # Haystack pipeline logic
│   │    ├── highlight_snippet_in_pdf.py       # PDF snippet highlighting
│   │    └── config.py                         # Environment configuration
│   └── models/
│        └── schema.py                         # Request/response Pydantic models
├── pipelines/
│   └── indexing_pipeline.py                   # Script to index PDFs
├── documenten-import/                         # Input PDF files
├── output/
│   └── highlighted/                           # Output PDFs with highlights
├── chat-api.py                                # Alternative FastAPI implementation
├── chat-local-llm-ranker.py                   # Streamlit interface
├── requirements.txt                           # Python dependencies
├── docker-compose.yml                         # Docker services setup
├── Dockerfile                                 # Docker app container
├── .env.local                                 # Environment variables
└── README.md                                  # Project documentation
```

### Key Technologies

- **Haystack 2.11+**: RAG framework
- **FastAPI**: Modern web framework
- **Elasticsearch 8.11.1**: Search and vector database
- **Sentence Transformers**: Multilingual embeddings
- **Rich**: Enhanced logging and console output
- **Streamlit**: Web interface framework

## Deployment

### Docker Deployment

The project includes Docker configuration for easy deployment:

```bash
# Start Elasticsearch
docker-compose up -d

# Build and run the application (customize as needed)
docker build -t mijndavi-rag .
docker run -p 8000:8000 --env-file .env.local mijndavi-rag
```

### Production Considerations

- Use proper Elasticsearch security in production
- Configure SSL/TLS for API endpoints
- Implement proper authentication and authorization
- Set up monitoring and logging
- Scale Elasticsearch cluster as needed
- Consider using a reverse proxy (nginx, Traefik)

## Troubleshooting

### Common Issues

1. **Elasticsearch Connection Failed**
   - Ensure Elasticsearch is running: `docker-compose ps`
   - Check the `ELASTIC_HOST` configuration
   - Verify network connectivity

2. **No Documents Found**
   - Run the indexing pipeline: `python pipelines/indexing_pipeline.py`
   - Check if PDF files are in `documenten-import/` folder
   - Verify Elasticsearch index exists

3. **LLM Connection Issues**
   - Check `LLM_API_URL` configuration
   - Ensure local LLM server is running
   - Verify API key for OpenAI models

4. **Memory Issues**
   - Increase Docker memory limits
   - Reduce batch sizes in configuration
   - Consider using quantized models

### Logging

The application uses Rich logging for enhanced console output. Logs include:
- Request processing information
- Pipeline execution details
- Error messages with tracebacks
- Performance metrics

## License

© 2025 by MijnDAVI. All rights reserved.

---

## 🛠️ My Customized Commands

### Re-index PDFs:
```bash
python pipelines/indexing_pipeline.py
```

### Start API server (development):
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
