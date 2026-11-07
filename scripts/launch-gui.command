#!/bin/sh
set -eu
exec sh "$(dirname -- "$0")/eljeffe.sh" serve --open
