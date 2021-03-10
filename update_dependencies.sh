#!/bin/sh
export CUSTOM_COMPILE_COMMAND=./update_dependencies.sh
pip-compile constraints.in
pip-compile dev-requirements.in
pip-compile

# Remove the line specifying dallinger as editable dependency
sed -e "s/^-e.*//" -i *.txt
