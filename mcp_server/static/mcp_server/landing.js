// Botões "Copiar" da landing e do /dashboard/mcp/: copia o texto do alvo
// (data-copiar = id) e confirma no próprio botão. Arquivo estático porque a
// landing sai com CSP sem script inline.
(function () {
  document.addEventListener('click', function (ev) {
    var botao = ev.target.closest('[data-copiar]');
    if (!botao) return;
    var alvo = document.getElementById(botao.getAttribute('data-copiar'));
    if (!alvo || !navigator.clipboard) return;
    var rotulo = botao.querySelector('[data-rotulo]') || botao;
    var antes = rotulo.textContent;
    navigator.clipboard.writeText(alvo.textContent.trim()).then(function () {
      rotulo.textContent = 'Copiado';
      setTimeout(function () { rotulo.textContent = antes; }, 1600);
    });
  });
})();
