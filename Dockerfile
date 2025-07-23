FROM docker.elastic.co/elasticsearch/elasticsearch:8.11.1

# Configure Elasticsearch to run in single-node mode. Safe to run in development.
ENV discovery.type=single-node
ENV xpack.security.enabled=false
ENV ES_JAVA_OPTS=-Xms1024m
ENV network.host=127.0.0.1
ENV http.port=9200
ENV transport.port=9200
# Expose necessary ports
EXPOSE 9200 9200