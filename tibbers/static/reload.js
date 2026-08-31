/* Reload this page when the app says to.
 *
 * Injected into every page by the server, on every instance -- not only dev
 * ones -- because `deploy.sh --static` refreshes the windows of the LIVE app
 * this way, with no process restart at all.
 *
 * It is not a poll. One request is held open by the server until the reload
 * token moves or the timeout expires, so an idle page costs one request every
 * half minute; a page nobody has open costs nothing. In a dev instance the
 * same token also moves when a file under tibbers/static changes, which is
 * what makes editing the picker feel like editing a web page.
 *
 * The page rebuilds itself from /api/state after a reload, so the only thing
 * worth carrying across is which tab you were reading -- losing your place in
 * the build page every time a stylesheet changed would defeat the point.
 */
(function () {
  var TAB_KEY = 'tibbers.view';

  function saveTab() {
    try {
      var on = document.querySelector('.tab.on');
      if (on && on.dataset && on.dataset.view) {
        sessionStorage.setItem(TAB_KEY, on.dataset.view);
      }
    } catch (e) { /* private mode, or a page with no tabs */ }
  }

  function restoreTab() {
    try {
      var view = sessionStorage.getItem(TAB_KEY);
      // showView also replays the view's entrance animation, so the restored
      // tab arrives the same way a clicked one does.
      if (view && typeof window.showView === 'function') window.showView(view);
    } catch (e) { /* ditto */ }
  }

  // Bubble phase, so the tab button's own onclick has already run and .tab.on
  // is the tab you just moved to rather than the one you left.
  document.addEventListener('click', saveTab);
  window.addEventListener('pagehide', saveTab);
  restoreTab();

  var token = null;
  var failures = 0;

  function listen() {
    var url = '/api/reload' + (token === null ? '' : '?since=' + token);
    fetch(url, { cache: 'no-store' }).then(function (response) {
      if (!response.ok) throw new Error(response.status);
      return response.json();
    }).then(function (data) {
      failures = 0;
      if (token !== null && data.token !== token) {
        saveTab();
        location.reload();
        return;
      }
      token = data.token;
      listen();
    }).catch(function () {
      // The app has gone, or predates this endpoint. Back off, then give up
      // rather than hammering a port that is not answering.
      if (++failures > 5) return;
      setTimeout(listen, 2000 * failures);
    });
  }

  listen();
})();
