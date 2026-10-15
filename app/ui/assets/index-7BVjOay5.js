// Old UI bundle replaced — redirect to new BOMA interface
(function(){
  try {
    var dest = '/?v=' + Date.now();
    if (window.location.search.indexOf('v=') === -1) {
      window.location.replace(dest);
    }
  } catch(e) {}
})();
