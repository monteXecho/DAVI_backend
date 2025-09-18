#!/bin/bash
mongod --dbpath "$(pwd)/app/db" --bind_ip_all --port 27017
