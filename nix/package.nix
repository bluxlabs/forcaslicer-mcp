{ pkgs }:
let
  version = "2.3.2-fos.8.6.8";
  src = pkgs.fetchurl {
    name = "forcaslicer-${version}.AppImage";
    url = "https://github.com/jiyang1018/FOrcaSlicer/releases/download/v${version}/FOrcaSlicer_Linux_V${version}.AppImage";
    sha256 = "713686b0b9b9101a9cbdb39dd3f57d99b09fde9eafc0da394b5482e7d2471477";
  };
  extracted = pkgs.appimageTools.extract { pname = "forcaslicer-runtime"; inherit version src; };
  runtime = pkgs.appimageTools.wrapType2 {
    pname = "forcaslicer-runtime";
    inherit version src;
    extraPkgs = p: [ p.webkitgtk_4_1 p.glib-networking p.libglvnd p.stdenv.cc.cc.lib p.gst_all_1.gst-plugins-good p.gst_all_1.gst-plugins-bad ];
  };
  headless = pkgs.writeShellScriptBin "forcaslicer-headless" ''
    set -euo pipefail
    export GDK_BACKEND=x11 LIBGL_ALWAYS_SOFTWARE=1 GTK_USE_PORTAL=0
    export WEBKIT_DISABLE_COMPOSITING_MODE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1
    export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
    export PATH=${pkgs.lib.makeBinPath [ pkgs.xorg-server pkgs.xauth pkgs.coreutils pkgs.gnugrep pkgs.util-linux ]}:$PATH
    # xvfb-run owns a private display and Xauthority; it never connects to DISPLAY.
    exec ${pkgs.xvfb-run}/bin/xvfb-run -a -s '-screen 0 1280x800x24 -nolisten tcp' \
      ${runtime}/bin/forcaslicer-runtime "$@"
  '';
  python = pkgs.python3.withPackages (p: [ p.mcp ]);
  source = pkgs.runCommand "forcaslicer-mcp-source" {} ''
    mkdir -p $out
    cp ${../server.py} $out/server.py
    cp ${../gcode_audit.py} $out/gcode_audit.py
  '';
  audit = pkgs.writeShellScriptBin "forcaslicer-audit" ''
    exec ${python}/bin/python ${source}/gcode_audit.py "$@"
  '';
  server = pkgs.writeShellScriptBin "forcaslicer-mcp" ''
    export FORCASLICER_CLI=${headless}/bin/forcaslicer-headless
    exec ${python}/bin/python ${source}/server.py "$@"
  '';
in pkgs.symlinkJoin {
  name = "forcaslicer-mcp-0.2.0";
  paths = [ server headless audit ];
  passthru = { inherit runtime extracted python; };
  meta = { description = "FOrcaSlicer and U1 Moonraker MCP with isolated background slicing"; platforms = [ "x86_64-linux" ]; };
}
