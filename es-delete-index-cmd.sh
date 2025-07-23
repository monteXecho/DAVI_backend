curl -X POST "http://localhost:9200/haystack_test/_delete_by_query?pretty" \
     -H 'Content-Type: application/json' \
     -d'
{
  "query": {
    "match_all": {}
  }
}
'