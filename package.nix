{ writeShellScriptBin, python3 }:

writeShellScriptBin "ausweis-sak-advertise" ''
  exec ${python3}/bin/python3 ${./advertise.py} "$@"
''
