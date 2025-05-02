
  $(document).ready(function () {
    var lastQueries = [];
    var lastResponses = [];
    var lastQuery = "";

    $("#toggle-upload").change(function () {
      $("#upload-container").slideToggle(300, function () {
        $(".query-response-container").toggleClass("full-height", !$("#upload-container").is(":visible"));
      });
    });

    $(".message-box").fadeIn().delay(3000).fadeOut();

    // AJAX Query Form Submission
    $("#query-form").submit(function (event) {
      event.preventDefault();
      var queryText = $("#query").val().trim();
      $("#query").val("");

      if (!queryText) {
        alert("Please enter a question.");
        return;
      }

      lastQuery = queryText;
      $("#chat-loader").show();

      var csrfToken = $("input[name='csrfmiddlewaretoken']").val();

      $.ajax({
        url: "{% url 'index' %}",
        type: "POST",
        data: { query: queryText, csrfmiddlewaretoken: csrfToken },
        dataType: "json",
        success: function (response) {
          var answer = response.answer || "No answer received.";

          // Store only the last 3 queries & responses
          if (lastQueries.length >= 3) {
            lastQueries.shift();
            lastResponses.shift();
          }
          lastQueries.push(queryText);
          lastResponses.push(answer);

          updateChatHistory();
          loadSourceDocuments(); // Auto-fetch source documents
          $("#message-response").text("Response received successfully!").fadeIn().delay(3000).fadeOut();
        },
        error: function () {
          alert("An error occurred. Please try again.");
        },
        complete: function () {
          $("#chat-loader").hide();
        },
      });
    });

    function updateChatHistory() {
      var historyContainer = $("#chat-history");
      historyContainer.children(":not(#chat-loader)").remove();

      lastQueries.forEach((query, i) => {
        historyContainer.append(`
          <div class="chat-message user">
            <div class="chat-bubble chat-user">Question: ${query}</div>
          </div>
          <div class="chat-message bot">
            <div class="chat-bubble chat-bot">Answer: ${lastResponses[i]}</div>
          </div>
        `);
      });

      historyContainer.animate({ scrollTop: historyContainer.prop("scrollHeight") }, 500);
    }

    function loadSourceDocuments() {
      if (!lastQuery) return;

      var csrfToken = $("input[name='csrfmiddlewaretoken']").val();

      $.ajax({
        url: "{% url 'index' %}",
        type: "POST",
        data: {
          fetch: true,
          query: lastQuery,
          csrfmiddlewaretoken: csrfToken,
        },
        dataType: "json",
        success: function (response) {
          $("#source-docs-container").fadeIn();
          $("#source-docs-list").empty();

          var documents = response.documents || [];

          if (documents.length === 0) {
            $("#source-docs-list").html('<p class="no-docs-message"> No relevant source documents found.</p>');
            return;
          }

          documents.forEach(function (doc, index) {
            let docTitle = doc.title || `Document ${index + 1}`;
            let docSource = doc.source || "Unknown Source";
            let filePath = doc.file_path || "#";

            $("#source-docs-list").append(`
              <div class="document-item">
                <p class="doc-title">${index + 1}. <strong>${docTitle}</strong></p>
                <p class="doc-source">Source Type: ${docSource}</p>
                <p><a href="${filePath}" target="_blank" class="doc-link">🔗 Open Document</a></p>
              </div>
            `);
          });
        },
        error: function () {
          alert("Error fetching source documents. Please try again.");
        },
      });
    }
  });

