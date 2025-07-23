#!/usr/bin/env python3

# This script indexes all .pdf files in the folder "documenten-import" into an
# ElasticSearch document store using a Haystack 2.12 pipeline.
# (c) 2025 by Rick Hoekman

import glob
import os
from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore
from haystack import Pipeline
from haystack.components.embedders import SentenceTransformersDocumentEmbedder
# from haystack.components.converters import TextFileToDocument
from haystack.components.converters import PDFMinerToDocument
from haystack.components.preprocessors import DocumentCleaner
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter 
from haystack.document_stores.errors import DuplicateDocumentError  # Import the error
from rich import print

document_store = ElasticsearchDocumentStore(
    hosts="http://localhost:9200",
    http_auth=("elastic", "elastic"),
    verify_certs=False,
    index="haystack_test"
)

converter = PDFMinerToDocument()
cleaner = DocumentCleaner()
splitter = DocumentSplitter(split_by="word", split_length=150, split_overlap=50)
# multi-qa-mpnet-base-dot-v1 vervangen door paraphrase-multilingual-mpnet-base-v2 voor NL ondersteuning
doc_embedder = SentenceTransformersDocumentEmbedder(model="sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
writer = DocumentWriter(document_store)

indexing_pipeline = Pipeline()
indexing_pipeline.add_component("converter", converter)
indexing_pipeline.add_component("cleaner", cleaner)
indexing_pipeline.add_component("splitter", splitter)
indexing_pipeline.add_component("doc_embedder", doc_embedder)
indexing_pipeline.add_component("writer", writer)

indexing_pipeline.connect("converter", "splitter")
indexing_pipeline.connect("splitter", "cleaner")
indexing_pipeline.connect("cleaner", "doc_embedder")
indexing_pipeline.connect("doc_embedder", "writer")

# Use glob to list all .pdf files in the folder "documenten-import"
# Please note that only .pdf files are supported by the PyPDFToDocument converter
file_paths = glob.glob("documenten-import/*.pdf")
print(f"Found {len(file_paths)} files to index.")
print(file_paths)
if file_paths:
    for file_path in file_paths:
        try:
            indexing_pipeline.run({
                "converter": {"sources": [file_path]}
            })
            print(f"Document {os.path.basename(file_path)} placed in the document store.")
        except DuplicateDocumentError:
            print(f"Document {os.path.basename(file_path)} already exist in the document store.")
else:
    print("No files to index.")

# List the index "haystack_test" details using the underlying Elasticsearch client
index_info = document_store.client.indices.get(index="haystack_test")
print("Index information for 'haystack_test':")
print(index_info)
