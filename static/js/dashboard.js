/* Rafraîchit les indicateurs du tableau de bord sans recharger la page.
 *
 * Même parti pris que compteur-alerte.js : écrit à la main, sans
 * bibliothèque, en ES5. Il s'agit de lire un JSON et de récrire du texte —
 * charger un framework pour cela coûterait plus de kilo-octets que toute
 * l'application, et obligerait à le servir en local comme le reste.
 *
 * Trois règles portent ce fichier.
 *
 * 1. Le serveur a déjà écrit les valeurs dans la page. Ce script ne construit
 *    rien au premier passage : il remplace. Un navigateur sans JavaScript
 *    affiche donc un tableau de bord juste, simplement figé.
 *
 * 2. Il ne formate aucun nombre. « 13 584 » et « 31,4 » arrivent déjà en
 *    français depuis la vue (_indicateurs_affichables). Reformater ici
 *    reviendrait à écrire deux fois la même règle, dans deux langages, et à
 *    les voir diverger au premier arrondi.
 *
 * 3. Il n'écrit jamais de HTML. textContent et createElement uniquement :
 *    innerHTML sur une donnée venue du serveur — une plaque
 *    d'immatriculation saisie par un utilisateur, par exemple — ferait de ce
 *    script une faille XSS. Le bandeau d'alertes, lui, pose bien du HTML,
 *    mais parce que Django le lui envoie déjà échappé.
 */
(function () {
  "use strict";

  /* Dix secondes : le compromis demandé. Assez court pour qu'un camion qui
   * se met en route se voie, assez long pour que la page coûte six requêtes
   * SQL toutes les dix secondes et non toutes les secondes. */
  var INTERVALLE = 10 * 1000;

  var conteneur = document.getElementById("tableau-de-bord");
  if (!conteneur) {
    /* Le script est chargé par le seul gabarit du tableau de bord, mais
     * mieux vaut sortir proprement que lever une erreur le jour où il sera
     * inclus ailleurs. */
    return;
  }

  var url = conteneur.getAttribute("data-kpi-url");
  if (!url) {
    return;
  }

  /* Remplace valeur, unité, ligne d'aide et couleur de chaque carte.
   *
   * Chaque indicateur est retrouvé par sa clé, posée en data-kpi* par
   * partials/_carte_kpi.html. Une carte absente de la page est simplement
   * ignorée : ajouter un indicateur côté serveur ne casse donc rien ici, et
   * en retirer un non plus.
   */
  function appliquerIndicateurs(indicateurs) {
    for (var index = 0; index < indicateurs.length; index += 1) {
      var indicateur = indicateurs[index];
      var cle = indicateur.cle;

      var valeur = conteneur.querySelector('[data-kpi="' + cle + '"]');
      if (valeur) {
        valeur.textContent = indicateur.valeur;
      }

      var unite = conteneur.querySelector('[data-kpi-unite="' + cle + '"]');
      if (unite) {
        unite.textContent = indicateur.unite;
      }

      var aide = conteneur.querySelector('[data-kpi-aide="' + cle + '"]');
      if (aide) {
        aide.textContent = indicateur.aide;
      }

      /* La couleur de la carte fait partie de l'information : une échéance
       * qui vient d'être dépassée doit faire rougir la carte sans qu'on
       * recharge. On retire les deux variantes puis on pose celle du
       * serveur, au lieu de basculer l'une sans l'autre. */
      var carte = conteneur.querySelector('[data-kpi-carte="' + cle + '"]');
      if (carte) {
        carte.classList.remove("ff-kpi--avant", "ff-kpi--alerte");
        if (indicateur.variante) {
          carte.classList.add("ff-kpi--" + indicateur.variante);
        }
      }
    }
  }

  /* Reconstruit la liste des jauges de kilométrage.
   *
   * Celle-ci est reconstruite et non mise à jour en place, parce que son
   * contenu change de nature : l'ordre des camions bouge (la liste est triée
   * du plus roulant au moins roulant) et un camion peut y entrer en cours de
   * journée, au premier relevé qui le fait avancer.
   */
  function appliquerKilometres(lignes) {
    var liste = conteneur.querySelector("[data-km-liste]");
    if (!liste) {
      return;
    }

    /* Laisser l'état du serveur en place plutôt que de vider la carte : une
     * réponse inattendue ne doit pas effacer ce qui était juste. */
    if (!lignes || !lignes.length) {
      return;
    }

    var fragment = document.createDocumentFragment();
    for (var index = 0; index < lignes.length; index += 1) {
      var ligne = lignes[index];

      var rangee = document.createElement("div");
      rangee.className = "ff-jauge";

      var libelle = document.createElement("a");
      libelle.className = "ff-jauge__libelle ff-chiffre";
      libelle.setAttribute("href", ligne.url);
      libelle.textContent = ligne.immatriculation;

      var piste = document.createElement("span");
      piste.className = "ff-jauge__piste";
      var remplissage = document.createElement("span");
      remplissage.className = "ff-jauge__remplissage";
      /* La largeur est la seule valeur qui finit dans du CSS : on la borne à
       * 0–100 et on la force en nombre, pour qu'aucune chaîne venue du
       * réseau n'atterrisse telle quelle dans un attribut de style. */
      var part = Math.max(0, Math.min(100, Number(ligne.part) || 0));
      remplissage.style.width = part + "%";
      piste.appendChild(remplissage);

      var valeur = document.createElement("span");
      valeur.className = "ff-jauge__valeur ff-chiffre";
      valeur.textContent = ligne.km + " km";

      rangee.appendChild(libelle);
      rangee.appendChild(piste);
      rangee.appendChild(valeur);
      fragment.appendChild(rangee);
    }

    liste.textContent = "";
    liste.appendChild(fragment);
  }

  function appliquer(donnees) {
    if (donnees.indicateurs) {
      appliquerIndicateurs(donnees.indicateurs);
    }
    appliquerKilometres(donnees.km_par_camion);

    var mesure = document.querySelector("[data-kpi-mesure]");
    if (mesure && donnees.mesure_le) {
      mesure.textContent = donnees.mesure_le;
    }
  }

  function rafraichir() {
    /* La pause demandée : un onglet caché ne consomme rien. Un tableau de
     * bord laissé ouvert dans un onglet d'arrière-plan toute la journée,
     * c'est 8 640 appels pour personne — et six requêtes SQL chacun. */
    if (document.hidden) {
      return;
    }

    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (reponse) {
        /* Une redirection vers la page de connexion renvoie du HTML avec un
         * code 200 : on refuse donc ce qui n'est pas du JSON, au lieu de
         * laisser .json() lever. */
        var type = reponse.headers.get("Content-Type") || "";
        if (!reponse.ok || type.indexOf("application/json") === -1) {
          return null;
        }
        return reponse.json();
      })
      .then(function (donnees) {
        if (donnees) {
          appliquer(donnees);
        }
      })
      .catch(function () {
        /* Hors ligne ou serveur arrêté : on garde l'affichage courant. Des
         * chiffres un peu périmés valent mieux qu'une page vidée — et
         * l'heure de relevé qui ne bouge plus le dit déjà au lecteur. */
      });
  }

  window.setInterval(rafraichir, INTERVALLE);

  /* Au retour sur l'onglet, on ne fait pas attendre dix secondes de plus :
   * la page vient peut-être de passer une heure sans rien recevoir. */
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      rafraichir();
    }
  });
})();
