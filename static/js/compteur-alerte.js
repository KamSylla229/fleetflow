/* Met à jour en continu les durées « il y a ... » des alertes.
 *
 * Écrit à la main, sans bibliothèque : il s'agit de relire une date et de
 * récrire un texte toutes les secondes. Charger un outil de formatage de
 * dates pour cela coûterait plus de kilo-octets que toute l'application.
 *
 * Le principe : chaque élément concerné porte un attribut data-depuis
 * contenant une date ISO 8601 (produite par le filtre |date:"c" de Django).
 * Le navigateur sait lire ce format seul, et comme la date est absolue, le
 * décalage horaire du poste n'a aucune importance.
 *
 *     <span data-depuis="2026-10-01T09:20:00+01:00">3 h 40</span>
 *
 * Le serveur a déjà écrit une valeur dans l'élément : la page est donc juste
 * avant que ce script ne tourne, et reste lisible s'il ne tourne jamais.
 */
(function () {
  "use strict";

  var SECONDE = 1000;

  function formater(millisecondes) {
    var totalMinutes = Math.floor(millisecondes / 60000);
    if (totalMinutes < 1) {
      return "moins d'une minute";
    }
    if (totalMinutes < 60) {
      return totalMinutes + " min";
    }
    var heures = Math.floor(totalMinutes / 60);
    var minutes = totalMinutes % 60;
    // « 3 h 40 » et non « 3 h 40 min » : c'est la forme de la maquette, et
    // l'unité des minutes se devine après celle des heures.
    return heures + " h " + (minutes < 10 ? "0" + minutes : minutes);
  }

  function rafraichir() {
    var maintenant = Date.now();
    var elements = document.querySelectorAll("[data-depuis]");
    for (var index = 0; index < elements.length; index += 1) {
      var element = elements[index];
      var depuis = Date.parse(element.getAttribute("data-depuis"));
      // Date.parse renvoie NaN sur une valeur illisible : on laisse alors le
      // texte du serveur en place plutôt que d'afficher « NaN ».
      if (!isNaN(depuis)) {
        element.textContent = formater(maintenant - depuis);
      }
    }
  }

  /* Recharge le fragment des bandeaux depuis le serveur.
   *
   * Le compteur ci-dessus fait avancer une durée, mais il ne peut pas
   * *découvrir* une alerte apparue après le chargement de la page. D'où cette
   * interrogation périodique, volontairement lente : trente secondes
   * suffisent pour une panne de boîtier, et c'est trente fois moins de
   * requêtes qu'un rafraîchissement par seconde.
   */
  function rafraichirFragment() {
    var conteneur = document.getElementById("bandeau-alertes");
    if (!conteneur) {
      return;
    }
    var url = conteneur.getAttribute("data-rafraichir-url");
    if (!url) {
      return;
    }
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (reponse) {
        return reponse.ok ? reponse.text() : null;
      })
      .then(function (html) {
        if (html !== null) {
          conteneur.innerHTML = html;
          rafraichir();
        }
      })
      .catch(function () {
        /* Hors ligne ou serveur arrêté : on garde l'affichage courant plutôt
         * que de vider le bandeau. Une alerte qui disparaît à tort serait
         * plus grave qu'une alerte un peu périmée. */
      });
  }

  rafraichir();
  window.setInterval(rafraichir, SECONDE);
  window.setInterval(rafraichirFragment, 30 * SECONDE);
})();
