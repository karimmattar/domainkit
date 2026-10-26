// Starts Swagger UI for domainkit's /docs, with FastAPI's default options.
// It is a file, not an inline script, so the page's policy allows scripts
// from the API's own origin only. The page names the OpenAPI document's URL.
window.addEventListener("load", function () {
  var element = document.getElementById("swagger-ui");
  window.ui = SwaggerUIBundle({
    url: element.dataset.openapiUrl,
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  });
});
