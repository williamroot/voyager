/**
 * validacao_hotkeys.js — atalhos globais da fila de anotação.
 *
 * Escopado por <body data-page="leads-validacao-*">. Não interfere com o
 * Alpine inline do `_validacao_card.html` (que escuta `@keydown.window`):
 * coexistem porque ambos disparam `.click()` no mesmo botão (idempotente
 * via `data-hotkey`). Este arquivo prioriza acessibilidade — funciona
 * mesmo se o Alpine do card não carregar (degradação graciosa).
 *
 * Funções:
 *   - Atalhos 1/2/3/4/I/E/S → click no botão correspondente
 *   - `?`                   → toggle do modal de hotkeys
 *   - `Esc`                 → fecha modal (Alpine cuida)
 *   - `Ctrl+Z`              → desfazer última decisão (janela 2s)
 *   - Ignora foco em textarea/input/select
 */
(function () {
  'use strict';

  // Escopo: só rodar em páginas marcadas como leads-validacao-*.
  function isValidacaoPage() {
    var page = (document.body && document.body.dataset && document.body.dataset.page) || '';
    return page.indexOf('leads-validacao') === 0;
  }
  if (!isValidacaoPage()) return;

  var HOTKEYS = {
    '1': 'eh_precatorio',
    '2': 'eh_pre',
    '3': 'eh_dc',
    '4': 'nao_lead',
    'i': 'incerto',
    'e': 'precisa_enriquecer',
    's': 'skip',
  };

  // Stack de undo: cada entrada {timestamp, label, undoUrl?}.
  // Implementação best-effort: o servidor é APPEND-ONLY (UniqueConstraint),
  // logo o "undo" real só seria possível com uma rota dedicada. Aqui apenas
  // notifica o usuário via toast caso esteja dentro da janela.
  var UNDO_WINDOW_MS = 2000;
  var undoStack = [];

  function inEditable(el) {
    if (!el) return false;
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'input' || tag === 'select') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function clickDecisao(resultado) {
    var btn = document.querySelector(
      'button[name="resultado"][value="' + resultado + '"]'
    );
    if (btn && !btn.disabled) {
      btn.click();
      return true;
    }
    return false;
  }

  function showHotkeysToggle() {
    window.dispatchEvent(new CustomEvent('validacao:show-hotkeys'));
  }

  function attemptUndo() {
    var last = undoStack[undoStack.length - 1];
    if (!last) return false;
    if (Date.now() - last.timestamp > UNDO_WINDOW_MS) return false;
    undoStack.pop();
    // Sinaliza pra UI mostrar toast — implementação concreta no T15/T16.
    window.dispatchEvent(new CustomEvent('validacao:undo-requested', { detail: last }));
    return true;
  }

  document.addEventListener('keydown', function (e) {
    // Ignora foco em campo editável.
    if (inEditable(e.target) || inEditable(document.activeElement)) return;

    // Ctrl+Z → undo.
    if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z')) {
      if (attemptUndo()) {
        e.preventDefault();
      }
      return;
    }

    // Demais atalhos não usam modifiers.
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    var key = (e.key || '').toLowerCase();

    if (key === '?') {
      e.preventDefault();
      showHotkeysToggle();
      return;
    }

    if (Object.prototype.hasOwnProperty.call(HOTKEYS, key)) {
      if (clickDecisao(HOTKEYS[key])) {
        e.preventDefault();
      }
    }
  });

  // Observa salvamento bem-sucedido → empilha undo placeholder.
  document.body.addEventListener('htmx:afterRequest', function (e) {
    try {
      var url = e.detail && e.detail.xhr && e.detail.xhr.responseURL;
      var ok = e.detail && e.detail.successful;
      if (!url || !ok) return;
      if (url.indexOf('/leads/validacao/salvar/') !== -1) {
        undoStack.push({ timestamp: Date.now(), url: url });
        // Limita stack — só a última anotação é desfazível.
        if (undoStack.length > 5) undoStack.shift();
      }
    } catch (_err) {
      /* swallow */
    }
  });

  // Após swap do card, o Alpine reinicia — nada a fazer aqui.
})();
