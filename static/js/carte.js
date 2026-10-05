/* La carte de la flotte : un point par camion, déplacé sans recharger la page.
 *
 * Écrit dans le même style que compteur-alerte.js et dashboard.js : ES5, pas
 * de bibliothèque hors Leaflet, qui est servi depuis static/vendor.
 *
 * Quatre règles portent ce fichier. Les trois premières sont celles de
 * dashboard.js, la quatrième est propre à une carte.
 *
 * 1. Le serveur a déjà tout écrit. Les positions initiales arrivent avec la
 *    page, par une balise json_script : les points sont là dès l'ouverture,
 *    sans requête supplémentaire et sans carte vide le temps d'un aller-retour.
 *
 * 2. Aucun formatage de nombre ici. Vitesses et heures de relevé arrivent déjà
 *    mises en forme par views._camions_carte.
 *
 * 3. Jamais d'innerHTML sur une donnée du serveur. Les plaques et les noms de
 *    chauffeurs sont des saisies d'utilisateur : tout passe par textContent et
 *    createElement. Les popups et les étiquettes de Leaflet acceptent un
 *    élément DOM, et c'est ce qu'on leur donne — leur passer une chaîne la
 *    ferait interpréter comme du HTML.
 *
 * 4. **On ne recadre la carte qu'une seule fois.** C'est le piège propre au
 *    rafraîchissement d'une carte : recentrer à chaque passage arracherait la
 *    vue des mains de l'utilisateur en train de la déplacer ou de zoomer,
 *    toutes les dix secondes. Le cadrage automatique est un service au premier
 *    affichage ; après, c'est une nuisance.
 */
(function () {
  "use strict";

  var INTERVALLE = 10 * 1000;

  /* Les quatre tons du thème, et rien d'autre. Le ton vient du serveur et
   * finit dans un nom de classe CSS : on le valide contre cette liste plutôt
   * que de le concaténer de confiance. */
  var TONS = { vert: true, ambre: true, rouge: true, neutre: true };

  var conteneur = document.getElementById("carte-flotte");
  var hote = document.getElementById("plan");
  if (!conteneur || !hote || typeof L === "undefined") {
    /* Leaflet absent (fichier non servi, par exemple) : on laisse la page en
     * place. La liste des camions, rendue par Django, reste exacte et
     * utilisable — c'est tout l'intérêt de ne pas avoir mis les données
     * uniquement dans la carte. */
    return;
  }

  function lireJson(identifiant, defaut) {
    var balise = document.getElementById(identifiant);
    if (!balise) {
      return defaut;
    }
    try {
      return JSON.parse(balise.textContent);
    } catch (erreur) {
      return defaut;
    }
  }

  var url = conteneur.getAttribute("data-positions-url");
  var zoom = Number(conteneur.getAttribute("data-zoom")) || 7;
  var centre = lireJson("carte-centre", [9.3, 2.3]);
  var itineraires = lireJson("carte-itineraires", []);
  var camionsInitiaux = lireJson("carte-camions", []);

  /* Leaflet veut un conteneur vide : le message d'attente écrit par le
   * gabarit a fait son travail, il s'en va maintenant. */
  hote.textContent = "";

  var plan = L.map(hote, { center: centre, zoom: zoom });

  L.tileLayer(conteneur.getAttribute("data-tuiles-url"), {
    attribution: conteneur.getAttribute("data-tuiles-attribution"),
    maxZoom: 18,
  }).addTo(plan);

  /* Les axes parcourus, tracés une fois pour toutes. Ils ne bougent jamais :
   * ce sont des données de fleet/itineraires.py, pas des relevés.
   *
   * Le libellé passe par un élément DOM et non par une chaîne : bindTooltip
   * interprète une chaîne comme du HTML. Ici le libellé vient d'un module
   * Python et non d'une saisie, mais la règle vaut mieux sans exception —
   * c'est quand on en fait une que la suivante passe inaperçue. */
  function texte(valeur) {
    var element = document.createElement("span");
    element.textContent = valeur;
    return element;
  }

  for (var rang = 0; rang < itineraires.length; rang += 1) {
    L.polyline(itineraires[rang].points, {
      color: "#1B7A4B",
      weight: 3,
      opacity: 0.45,
    })
      .addTo(plan)
      .bindTooltip(texte(itineraires[rang].libelle));
  }

  var marqueurs = {};
  var cadrageFait = false;

  function icone(ton) {
    var sur = TONS[ton] ? ton : "neutre";
    return L.divIcon({
      className: "ff-marqueur",
      /* Chaîne constante, écrite ici : aucune donnée du serveur n'entre dans
       * ce HTML, seul le nom de ton validé plus haut. */
      html: '<span class="ff-marqueur__point ff-marqueur__point--' + sur + '"></span>',
      iconSize: [15, 15],
      iconAnchor: [7, 7],
      popupAnchor: [0, -8],
    });
  }

  /* Le contenu d'une bulle, construit élément par élément. */
  function bulle(camion) {
    var boite = document.createElement("div");

    var plaque = document.createElement("a");
    plaque.className = "ff-popup__plaque";
    plaque.setAttribute("href", camion.url);
    plaque.textContent = camion.immatriculation;
    boite.appendChild(plaque);

    var lignes = [camion.statut_libelle];
    if (camion.vitesse !== null) {
      lignes.push(camion.vitesse + " km/h");
    }
    if (camion.vu_le) {
      lignes.push("relevé à " + camion.vu_le);
    }
    if (camion.destination) {
      lignes.push("vers " + camion.destination + " · " + camion.progression + " %");
    }
    if (camion.chauffeur) {
      lignes.push(camion.chauffeur);
    }
    if (camion.signal_coupe) {
      lignes.push("boîtier coupé (démonstration)");
    }

    for (var index = 0; index < lignes.length; index += 1) {
      var ligne = document.createElement("span");
      ligne.className = "ff-popup__ligne";
      ligne.textContent = lignes[index];
      boite.appendChild(ligne);
    }
    return boite;
  }

  /* Place ou déplace les marqueurs, et retire ceux qui n'ont plus lieu d'être.
   *
   * Les marqueurs sont déplacés et non recréés : recréer ferait disparaître la
   * bulle ouverte par l'utilisateur à chaque passage, et perdrait l'animation
   * de déplacement.
   */
  function appliquerMarqueurs(camions) {
    var vus = {};
    var limites = [];

    for (var index = 0; index < camions.length; index += 1) {
      var camion = camions[index];
      if (camion.lat === null || camion.lon === null) {
        /* Un camion sans relevé n'a pas de place sur un fond de carte. Il
         * reste dans la liste de droite, avec son état « Aucune donnée ». */
        continue;
      }

      var position = [camion.lat, camion.lon];
      limites.push(position);
      vus[camion.id] = true;

      var marqueur = marqueurs[camion.id];
      if (marqueur) {
        marqueur.setLatLng(position);
        if (marqueur.ffTon !== camion.ton) {
          marqueur.setIcon(icone(camion.ton));
          marqueur.ffTon = camion.ton;
        }
        marqueur.setPopupContent(bulle(camion));
        marqueur.setTooltipContent(texte(camion.immatriculation));
      } else {
        marqueur = L.marker(position, { icon: icone(camion.ton) })
          .addTo(plan)
          .bindPopup(bulle(camion))
          /* Au survol, et non en permanence. Essayé en permanent d'abord,
           * et vu à l'écran : huit camions au départ de Cotonou donnent huit
           * étiquettes empilées et illisibles. Leaflet ne gère aucune
           * collision d'étiquettes, et le greffon qui le ferait serait une
           * dépendance de plus — hors cadre.
           *
           * La plaque se lit donc de trois autres façons : au survol du
           * point, dans la bulle au clic, et dans la liste de droite, qui les
           * donne toutes sans jamais se chevaucher. Sur la carte, ce qu'on
           * cherche d'un coup d'œil, c'est *où est la flotte* et de quelle
           * couleur elle est. */
          .bindTooltip(texte(camion.immatriculation), {
            direction: "top",
            offset: [0, -8],
            className: "ff-etiquette-plan",
          });
        marqueur.ffTon = camion.ton;
        marqueurs[camion.id] = marqueur;
      }
    }

    /* Un camion désactivé en cours de journée doit quitter la carte. */
    for (var identifiant in marqueurs) {
      if (Object.prototype.hasOwnProperty.call(marqueurs, identifiant) && !vus[identifiant]) {
        plan.removeLayer(marqueurs[identifiant]);
        delete marqueurs[identifiant];
      }
    }

    /* Le cadrage, une fois et une seule. Voir la règle 4 en tête de fichier. */
    if (!cadrageFait && limites.length) {
      plan.fitBounds(limites, { padding: [40, 40], maxZoom: 11 });
      cadrageFait = true;
    }
  }

  /* Reconstruit la liste de droite. Elle change d'ordre et de contenu : la
   * reconstruire est plus simple, et plus sûr, que de deviner ce qui a bougé.
   */
  function appliquerListe(camions) {
    var liste = conteneur.querySelector("[data-camions-liste]");
    if (!liste || !camions.length) {
      return;
    }

    var fragment = document.createDocumentFragment();
    for (var index = 0; index < camions.length; index += 1) {
      var camion = camions[index];

      var rangee = document.createElement("div");
      rangee.className = "ff-suivi";
      rangee.setAttribute("data-camion", camion.id);

      var ton = TONS[camion.ton] ? camion.ton : "neutre";
      var point = document.createElement("span");
      point.className = "ff-marqueur__point ff-marqueur__point--" + ton;
      rangee.appendChild(point);

      var corps = document.createElement("span");
      corps.className = "ff-suivi__corps";

      var plaque = document.createElement("a");
      plaque.className = "ff-chiffre ff-suivi__plaque";
      plaque.setAttribute("href", camion.url);
      plaque.textContent = camion.immatriculation;
      corps.appendChild(plaque);

      var details = [camion.statut_libelle];
      if (camion.signal_coupe) {
        details.push("boîtier coupé");
      }
      if (camion.destination) {
        details.push("vers " + camion.destination);
      }
      if (camion.chauffeur) {
        details.push(camion.chauffeur);
      }
      var sous = document.createElement("span");
      sous.className = "ff-sous-ligne";
      sous.textContent = details.join(" · ");
      corps.appendChild(sous);
      rangee.appendChild(corps);

      var mesures = document.createElement("span");
      mesures.className = "ff-suivi__mesures ff-chiffre";
      if (camion.vitesse !== null) {
        var vitesse = document.createElement("span");
        vitesse.className = "ff-suivi__vitesse";
        vitesse.textContent = camion.vitesse + " km/h";
        mesures.appendChild(vitesse);
      }
      if (camion.vu_le) {
        var heure = document.createElement("span");
        heure.className = "ff-sous-ligne";
        heure.textContent = camion.vu_le;
        mesures.appendChild(heure);
      }
      rangee.appendChild(mesures);

      fragment.appendChild(rangee);
    }

    liste.textContent = "";
    liste.appendChild(fragment);
  }

  function appliquerCompteurs(compteurs) {
    if (!compteurs) {
      return;
    }
    for (var code in compteurs) {
      if (Object.prototype.hasOwnProperty.call(compteurs, code)) {
        var element = document.querySelector('[data-carte-compteur="' + code + '"]');
        if (element) {
          element.textContent = compteurs[code];
        }
      }
    }
  }

  function appliquer(donnees) {
    if (!donnees || !donnees.camions) {
      return;
    }
    appliquerMarqueurs(donnees.camions);
    appliquerListe(donnees.camions);
    appliquerCompteurs(donnees.compteurs);

    var mesure = document.querySelector("[data-carte-mesure]");
    if (mesure && donnees.mesure_le) {
      mesure.textContent = donnees.mesure_le;
    }
  }

  /* Cliquer une ligne de la liste amène à son camion sur le plan. Le
   * gestionnaire est posé une fois sur le conteneur et non sur chaque ligne :
   * la liste est reconstruite toutes les dix secondes, et des écouteurs posés
   * ligne par ligne disparaîtraient avec elles. */
  conteneur.addEventListener("click", function (evenement) {
    var rangee = evenement.target.closest ? evenement.target.closest("[data-camion]") : null;
    if (!rangee) {
      return;
    }
    /* Un clic sur la plaque est un lien vers la fiche du camion : on le laisse
     * faire son travail. */
    if (evenement.target.tagName === "A") {
      return;
    }
    var marqueur = marqueurs[rangee.getAttribute("data-camion")];
    if (!marqueur) {
      return;
    }
    plan.panTo(marqueur.getLatLng());
    marqueur.openPopup();

    var actives = conteneur.querySelectorAll(".ff-suivi--actif");
    for (var index = 0; index < actives.length; index += 1) {
      actives[index].classList.remove("ff-suivi--actif");
    }
    rangee.classList.add("ff-suivi--actif");
  });

  function rafraichir() {
    /* Onglet caché : rien. Une carte laissée ouverte en arrière-plan toute la
     * journée, ce sont des milliers d'appels pour personne. */
    if (document.hidden || !url) {
      return;
    }

    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (reponse) {
        /* Une session expirée renvoie la page de connexion : du HTML, avec un
         * code 200. On refuse ce qui n'est pas du JSON plutôt que de laisser
         * .json() lever. */
        var type = reponse.headers.get("Content-Type") || "";
        if (!reponse.ok || type.indexOf("application/json") === -1) {
          return null;
        }
        return reponse.json();
      })
      .then(appliquer)
      .catch(function () {
        /* Hors ligne : on garde les derniers points connus. Des camions à leur
         * position d'il y a une minute valent mieux qu'une carte vidée — et
         * l'heure de relevé, qui ne bouge plus, le dit au lecteur. */
      });
  }

  /* Les positions arrivées avec la page, appliquées tout de suite. */
  appliquer({ camions: camionsInitiaux });

  window.setInterval(rafraichir, INTERVALLE);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      rafraichir();
    }
  });
})();
