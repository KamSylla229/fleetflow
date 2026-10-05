/* La carte de la flotte : un dessin SVG maison, sans aucune bibliothèque.
 *
 * Même parti pris que compteur-alerte.js et dashboard.js : ES5, écrit à la
 * main. Ce fichier a remplacé Leaflet, qui coûtait 162 Ko, imposait ses
 * icônes par défaut et dépendait d'un serveur de tuiles — pour un dessin que
 * cinq fonctions suffisent à produire.
 *
 * Cinq règles portent ce fichier.
 *
 * 1. **Aucune géographie ici.** Les axes, les villes et les camions arrivent
 *    déjà projetés en coordonnées du dessin par fleet/itineraires.py. Le
 *    script ne connaît ni latitude, ni cosinus. Projeter des deux côtés
 *    reviendrait à écrire la même formule dans deux langages, et à les voir
 *    diverger.
 *
 * 2. **Aucune couleur ici.** Tout est dans fleetflow.css, section 18 quater.
 *    Le script ne pose que des classes et des coordonnées.
 *
 * 3. **Aucune donnée en dur ici.** Pas une ville, pas une route : tout vient
 *    des balises json_script écrites par le gabarit.
 *
 * 4. **Jamais d'innerHTML sur une donnée du serveur.** Les plaques sont des
 *    saisies d'utilisateur : textContent et createElementNS uniquement.
 *
 * 5. **Les camions sont déplacés, jamais recréés.** C'est ce qui permet à la
 *    transition CSS de les faire glisser d'une position à l'autre, et ce qui
 *    conserve la sélection de l'utilisateur d'un rafraîchissement au suivant.
 */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";

  /* Cinq secondes entre deux relevés, quatre secondes de glissement : la
   * transition a le temps de finir avant la suivante, et les camions sont
   * donc presque toujours en mouvement à l'écran. Un déplacement instantané
   * toutes les cinq secondes donnerait une carte qui sautille. */
  var INTERVALLE = 5 * 1000;

  /* Les quatre tons du thème, et rien d'autre. Le ton vient du serveur et
   * finit dans un nom de classe : on le valide contre cette liste plutôt que
   * de le concaténer de confiance. */
  var TONS = { vert: true, ambre: true, rouge: true, neutre: true };

  /* Rayons, en unités du dessin. Ce sont des géométries, pas des couleurs :
   * elles n'auraient aucun sens dans la feuille de style, où l'attribut r
   * n'est d'ailleurs pas également pris en charge par tous les navigateurs.
   * Les épaisseurs de trait, elles, sont bien en CSS. */
  var RAYON_VILLE = 4;
  var RAYON_CAMION = 7;
  var RAYON_PULSATION = 13;
  var RAYON_SELECTION = 17;
  var DECALAGE_NOM = { x: 9, y: 4 };
  var DECALAGE_PLAQUE = { x: 13, y: 4 };

  var conteneur = document.getElementById("carte-flotte");
  var hote = document.getElementById("plan");
  if (!conteneur || !hote) {
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
  var geometrie = lireJson("carte-geometrie", null);
  var camionsInitiaux = lireJson("carte-camions", []);

  if (!geometrie) {
    /* Sans décor, pas de dessin — mais la liste de droite, rendue par Django,
     * reste exacte et utilisable. C'est tout l'intérêt de ne pas avoir mis
     * les données uniquement dans la carte. */
    return;
  }

  function creer(nom, classe) {
    var element = document.createElementNS(SVG, nom);
    if (classe) {
      /* setAttribute et non .className : sur un élément SVG, className est un
       * SVGAnimatedString en lecture seule. */
      element.setAttribute("class", classe);
    }
    return element;
  }

  function poser(element, attributs) {
    for (var nom in attributs) {
      if (Object.prototype.hasOwnProperty.call(attributs, nom)) {
        element.setAttribute(nom, attributs[nom]);
      }
    }
    return element;
  }

  function ton(valeur) {
    return TONS[valeur] ? valeur : "neutre";
  }

  // --- Le décor, dessiné une fois -------------------------------------------

  hote.textContent = "";

  var svg = poser(creer("svg", "ff-plan__svg"), {
    viewBox: "0 0 " + geometrie.largeur + " " + geometrie.hauteur,
    /* « meet » et non « slice ». Essayé avec slice d'abord, et vu à l'écran :
     * le cadre de la page n'a jamais exactement les proportions du dessin, et
     * slice rogne la différence — Parakou et Lokossa, aux deux extrémités,
     * disparaissaient. Un camion hors du cadre sur une carte de flotte, c'est
     * le seul défaut qu'on ne peut pas se permettre. « meet » laisse plutôt
     * deux bandes, que le dégradé du conteneur remplit sans qu'on les voie. */
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
    "aria-label": "Carte de la flotte",
  });

  svg.appendChild(
    poser(creer("rect", "ff-plan__terre"), {
      x: 0,
      y: 0,
      width: geometrie.largeur,
      height: geometrie.hauteur,
    })
  );

  /* Le golfe : un rectangle sous le trait de côte, dont l'ordonnée est
   * calculée par fleet/itineraires.py à partir de la ville la plus
   * méridionale. Un test garantit qu'aucun point d'itinéraire n'y tombe. */
  svg.appendChild(
    poser(creer("rect", "ff-plan__eau"), {
      x: 0,
      y: geometrie.cote_y,
      width: geometrie.largeur,
      height: Math.max(geometrie.hauteur - geometrie.cote_y, 0),
    })
  );

  /* Chaque axe en deux tracés superposés : un contour large et pâle, puis un
   * trait plus fin par-dessus. C'est ce qui donne l'épaisseur d'une route
   * plutôt que celle d'un trait de crayon — et c'est la seule façon de
   * l'obtenir sans filtre ni dégradé. */
  var routes = creer("g", "ff-plan__routes");
  for (var rang = 0; rang < geometrie.itineraires.length; rang += 1) {
    var trace = geometrie.itineraires[rang];
    var sommets = trace.points
      .map(function (point) {
        return point[0] + "," + point[1];
      })
      .join(" ");

    routes.appendChild(poser(creer("polyline", "ff-route__contour"), { points: sommets }));
    routes.appendChild(poser(creer("polyline", "ff-route__trait"), { points: sommets }));
  }
  svg.appendChild(routes);

  var villes = creer("g", "ff-plan__villes");
  for (var index = 0; index < geometrie.villes.length; index += 1) {
    var ville = geometrie.villes[index];

    villes.appendChild(
      poser(creer("circle", "ff-ville__point"), {
        cx: ville.x,
        cy: ville.y,
        r: RAYON_VILLE,
      })
    );

    var nom = poser(creer("text", "ff-ville__nom"), {
      x: ville.x + DECALAGE_NOM.x,
      y: ville.y + DECALAGE_NOM.y,
    });
    nom.textContent = ville.nom;
    villes.appendChild(nom);
  }
  svg.appendChild(villes);

  var calqueCamions = creer("g", "ff-plan__camions");
  svg.appendChild(calqueCamions);

  hote.appendChild(svg);

  // --- Les camions ----------------------------------------------------------

  var groupes = {};
  var choisi = null;

  function creerCamion(camion) {
    var groupe = creer("g", "ff-camion ff-camion--pose");
    groupe.setAttribute("data-camion", camion.id);

    /* La pulsation est toujours créée, et seulement activée par une classe :
     * un camion qui s'arrête ne doit pas faire recréer son groupe, sans quoi
     * on perdrait sa transition et la sélection de l'utilisateur. */
    groupe.appendChild(poser(creer("circle", "ff-camion__pulsation"), { r: RAYON_PULSATION }));
    groupe.appendChild(poser(creer("circle", "ff-camion__selection"), { r: RAYON_SELECTION }));
    groupe.appendChild(poser(creer("circle", "ff-camion__point"), { r: RAYON_CAMION }));

    var plaque = poser(creer("text", "ff-camion__plaque"), {
      x: DECALAGE_PLAQUE.x,
      y: DECALAGE_PLAQUE.y,
    });
    plaque.textContent = camion.immatriculation;
    groupe.appendChild(plaque);

    calqueCamions.appendChild(groupe);
    return groupe;
  }

  function classesCamion(camion) {
    var classes = "ff-camion ff-camion--" + ton(camion.ton);
    /* La pulsation marque le mouvement, pas la couleur : un camion à l'arrêt
     * est vert sur certaines flottes, et il ne doit pas battre. */
    if (camion.statut === "en_route") {
      classes += " ff-camion--anime";
    }
    if (String(camion.id) === choisi) {
      classes += " ff-camion--choisi";
    }
    return classes;
  }

  function placerCamions(camions) {
    var vus = {};
    var nouveaux = [];

    for (var index = 0; index < camions.length; index += 1) {
      var camion = camions[index];
      if (camion.x === null || camion.y === null) {
        /* Un camion sans relevé n'a pas de place sur le dessin. Il reste dans
         * la liste de droite, avec son état « Aucune donnée ». */
        continue;
      }
      vus[camion.id] = true;

      var groupe = groupes[camion.id];
      if (!groupe) {
        groupe = creerCamion(camion);
        groupes[camion.id] = groupe;
        nouveaux.push(groupe);
      }

      groupe.setAttribute("class", classesCamion(camion));
      /* style.transform et non l'attribut transform : seul le premier
       * s'anime. L'attribut SVG saute d'une position à l'autre, quelle que
       * soit la transition déclarée. */
      groupe.style.transform = "translate(" + camion.x + "px, " + camion.y + "px)";
    }

    /* Les groupes créés à l'instant portent ff-camion--pose, qui coupe la
     * transition : un camion qui apparaît doit se poser à sa place, pas
     * glisser depuis le coin supérieur gauche du dessin. La classe est
     * retirée à la frame suivante, une fois la position initiale peinte. */
    if (nouveaux.length) {
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () {
          for (var rang = 0; rang < nouveaux.length; rang += 1) {
            nouveaux[rang].setAttribute(
              "class",
              nouveaux[rang].getAttribute("class").replace(" ff-camion--pose", "")
            );
          }
        });
      });
    }

    /* Un camion désactivé en cours de journée doit quitter le dessin. */
    for (var identifiant in groupes) {
      if (Object.prototype.hasOwnProperty.call(groupes, identifiant) && !vus[identifiant]) {
        calqueCamions.removeChild(groupes[identifiant]);
        delete groupes[identifiant];
      }
    }
  }

  // --- Le compteur des boîtiers muets ---------------------------------------

  /* Âge des relevés tel que le serveur l'a mesuré, et l'instant où la réponse
   * est arrivée. Le compteur affiche la somme des deux : on ne demande jamais
   * l'heure au poste de l'utilisateur, qui peut être fausse de plusieurs
   * heures sans que personne ne s'en doute. Seul l'écoulement local est lu, et
   * une horloge fausse s'écoule à la bonne vitesse. */
  var instantReponse = Date.now();

  function formaterAge(secondes) {
    var minutes = Math.floor(secondes / 60);
    if (minutes < 1) {
      return "il y a moins d'une minute";
    }
    if (minutes < 60) {
      return "il y a " + minutes + " min";
    }
    var heures = Math.floor(minutes / 60);
    var reste = minutes % 60;
    return "il y a " + heures + " h " + (reste < 10 ? "0" + reste : reste);
  }

  function avancerCompteurs() {
    var ecoule = Math.floor((Date.now() - instantReponse) / 1000);
    var elements = conteneur.querySelectorAll("[data-age]");
    for (var index = 0; index < elements.length; index += 1) {
      var base = Number(elements[index].getAttribute("data-age"));
      if (!isNaN(base)) {
        elements[index].textContent = formaterAge(base + ecoule);
      }
    }
  }

  // --- La liste de droite ---------------------------------------------------

  function ligneCamion(camion) {
    var rangee = document.createElement("div");
    rangee.className = "ff-suivi" + (String(camion.id) === choisi ? " ff-suivi--actif" : "");
    rangee.setAttribute("data-camion", camion.id);

    var point = document.createElement("span");
    point.className = "ff-marqueur__point ff-marqueur__point--" + ton(camion.ton);
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
    if (camion.statut === "sans_signal" && camion.age_secondes !== null) {
      /* Pour un boîtier muet, ce qu'on veut lire n'est pas l'heure du dernier
       * contact mais depuis combien de temps il se tait. */
      var age = document.createElement("span");
      age.className = "ff-sous-ligne";
      age.setAttribute("data-age", camion.age_secondes);
      age.textContent = formaterAge(camion.age_secondes);
      mesures.appendChild(age);
    } else if (camion.vu_le) {
      var heure = document.createElement("span");
      heure.className = "ff-sous-ligne";
      heure.textContent = camion.vu_le;
      mesures.appendChild(heure);
    }
    rangee.appendChild(mesures);

    return rangee;
  }

  function appliquerListe(camions) {
    var liste = conteneur.querySelector("[data-camions-liste]");
    if (!liste || !camions.length) {
      return;
    }
    var fragment = document.createDocumentFragment();
    for (var index = 0; index < camions.length; index += 1) {
      fragment.appendChild(ligneCamion(camions[index]));
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
    instantReponse = Date.now();
    placerCamions(donnees.camions);
    appliquerListe(donnees.camions);
    appliquerCompteurs(donnees.compteurs);

    var mesure = document.querySelector("[data-carte-mesure]");
    if (mesure && donnees.mesure_le) {
      mesure.textContent = donnees.mesure_le;
    }
  }

  // --- La sélection ---------------------------------------------------------

  /* Le gestionnaire est posé une fois sur le conteneur et non sur chaque
   * ligne : la liste est reconstruite toutes les cinq secondes, et des
   * écouteurs posés ligne par ligne disparaîtraient avec elles. */
  conteneur.addEventListener("click", function (evenement) {
    var cible = evenement.target;
    var rangee = cible.closest ? cible.closest("[data-camion]") : null;
    if (!rangee) {
      return;
    }
    /* Un clic sur la plaque est un lien vers la fiche du camion : on le laisse
     * faire son travail. */
    if (cible.tagName === "A") {
      return;
    }

    var identifiant = rangee.getAttribute("data-camion");
    choisi = choisi === identifiant ? null : identifiant;
    marquerSelection();
  });

  function marquerSelection() {
    var lignes = conteneur.querySelectorAll(".ff-suivi");
    for (var index = 0; index < lignes.length; index += 1) {
      lignes[index].classList.toggle(
        "ff-suivi--actif",
        lignes[index].getAttribute("data-camion") === choisi
      );
    }
    for (var identifiant in groupes) {
      if (Object.prototype.hasOwnProperty.call(groupes, identifiant)) {
        var classes = groupes[identifiant].getAttribute("class").replace(" ff-camion--choisi", "");
        if (identifiant === choisi) {
          classes += " ff-camion--choisi";
        }
        groupes[identifiant].setAttribute("class", classes);
      }
    }
  }

  // --- Le rafraîchissement --------------------------------------------------

  function rafraichir() {
    /* Onglet caché : rien. Une carte laissée ouverte en arrière-plan toute la
     * journée, ce sont des milliers d'appels pour personne — et douze fois
     * plus souvent qu'au tableau de bord. */
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
        /* Hors ligne : on garde les dernières positions connues. Des camions
         * là où ils étaient il y a une minute valent mieux qu'une carte
         * vidée — et l'heure de relevé, qui ne bouge plus, le dit au lecteur.
         * Les compteurs « il y a… », eux, continuent d'avancer : c'est
         * exactement ce qu'on veut savoir quand la liaison est coupée. */
      });
  }

  /* Les positions arrivées avec la page, posées tout de suite. */
  appliquer({ camions: camionsInitiaux });
  marquerSelection();

  window.setInterval(rafraichir, INTERVALLE);
  window.setInterval(avancerCompteurs, 1000);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      rafraichir();
    }
  });
})();
