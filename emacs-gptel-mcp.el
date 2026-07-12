;;; emacs-gptel-mcp.el --- REVIEW BEFORE APPLYING  -*- lexical-binding: t; -*-
;;
;; Oracle: optional integration to use the SAME MCP tools (code graph, ripgrep,
;; git, emacs-read) inside a gptel buffer in Emacs, talking to your local Ollama.
;;
;; NOT added to init.el automatically — you said don't change Emacs on the fly.
;; To try it: read this, then eval-buffer (or paste into init.el). Requires the
;; `mcp' package (online, one-time): M-x package-install RET mcp RET
;;
;; The MCP bridges are already running as systemd user services:
;;   codebase-memory 9750 | source-grep 9751 | emacs 9752 | git 9753
;; gptel talks to them over SSE, same as RAGFlow does.
;;
;; Field caveat: gptel tool/agent mode is early — great for one-shot tool calls
;; (read a buffer, grep a repo, blame a line), shaky for long multi-step loops.

(use-package mcp
  :ensure t
  :after gptel
  :custom
  (mcp-hub-servers
   '(("source-grep" :url "http://localhost:9751/sse")
     ("git"         :url "http://localhost:9753/sse")
     ;; codebase-memory exposes 8 tools; add if you want graph queries in Emacs:
     ("codebase-memory" :url "http://localhost:9750/sse")))
  :config
  ;; Register the MCP tools with gptel. Run M-x mcp-hub-start-all-server first,
  ;; then gptel-mcp-connect (from the mcp gptel bridge) to expose them in
  ;; M-x gptel-tools. Disconnect with gptel-mcp-disconnect.
  (require 'gptel-integrations nil t))

;; Usage:
;;   M-x mcp-hub-start-all-server
;;   In a gptel buffer: M-x gptel-tools  (toggle the mcp tools on)
;;   Ask "grep serenedb for PgType and show it" -> it calls source_search/read_lines.
;;
;; The `emacs' MCP server (port 9752) reads buffers — you usually don't need it
;; from *inside* Emacs (gptel already has your buffers via gptel-add), so it's
;; omitted here; it's for RAGFlow. Add it if you want symmetry.

(provide 'emacs-gptel-mcp)
;;; emacs-gptel-mcp.el ends here
