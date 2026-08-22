for (const weight of ["regular", "thin", "light", "bold", "fill", "duotone"]) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `/static/phosphor/${weight}/style.css`;
  document.head.appendChild(link);
}
